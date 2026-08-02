from __future__ import annotations

from flowguard.services.import_classification import (
    build_label_inventory,
    classification_summary,
    project_validated_groups,
    redact_financial_numbers,
)


def _transactions() -> list[dict]:
    return [
        {
            "transaction_id": "transaction-secret-one",
            "account_id": "account-secret",
            "occurred_at": "2026-08-01T09:00:00+09:00",
            "direction": "OUTFLOW",
            "amount": 370_000,
            "description": "카드사 자동이체 08월 370,000원",
        },
        {
            "transaction_id": "transaction-secret-two",
            "account_id": "account-secret",
            "occurred_at": "2026-09-01T09:00:00+09:00",
            "direction": "OUTFLOW",
            "amount": 370_000,
            "description": "카드사 자동이체 09월 370,000원",
        },
    ]


def test_label_inventory_request_excludes_financial_fields_and_embedded_numbers() -> None:
    inventory = build_label_inventory(_transactions())

    payload = inventory.request_payload(import_id="import-opaque", locale="ko-KR")

    assert set(payload["labels"][0]) == {"labelId", "text", "direction", "occurrences"}
    serialized_labels = str(payload["labels"])
    assert "370000" not in serialized_labels
    assert "370,000" not in serialized_labels
    assert "2026-" not in serialized_labels
    assert "transaction-secret" not in serialized_labels
    assert "account-secret" not in serialized_labels
    assert all(
        not any(character.isdigit() for character in item["text"]) for item in payload["labels"]
    )


def test_validated_groups_are_projected_without_changing_transaction_facts() -> None:
    inventory = build_label_inventory(_transactions())
    request = inventory.request_payload(import_id="import-opaque", locale="ko-KR")
    label_ids = [item["labelId"] for item in request["labels"]]
    response = {
        **{key: request[key] for key in request if key != "labels"},
        "groups": [
            {
                "groupId": "group-card",
                "labelIds": label_ids,
                "normalizedName": "카드사 자동이체",
                "entityKind": "CARD_PAYMENT",
                "categoryHint": "CARD_BILL",
                "essentialHint": True,
                "confidence": "HIGH",
                "reason": "같은 카드 결제 항목입니다.",
            }
        ],
        "ungrouped": [],
    }

    groups = project_validated_groups(inventory, response)
    summary = classification_summary(inventory, response, applied=True)

    assert len(groups) == 1
    assert groups[0].classification is not None
    assert groups[0].classification["category_hint"] == "CARD_BILL"
    assert {item["amount"] for item in groups[0].transactions} == {370_000}
    assert summary == {
        "source": "AI",
        "applied": True,
        "original_label_count": 2,
        "grouped_entity_count": 1,
        "merged_label_count": 1,
    }


def test_redaction_removes_arabic_and_korean_amount_or_date_tokens() -> None:
    assert redact_financial_numbers("정산 2026-09-30 15만원") == "정산 <NUM>"
    assert redact_financial_numbers("팔월 결제 십만원") == "<NUM> 결제 <NUM>"
    assert redact_financial_numbers("이천이십육년 정산 삼십칠만") == "<NUM> 정산 <NUM>"
    assert redact_financial_numbers("서른만원 정산 한 달 뒤 세시") == ("<NUM> 정산 <NUM> 뒤 <NUM>")
    assert redact_financial_numbers("열한 원 결제 열두 달 뒤 열세시") == (
        "<NUM> 결제 <NUM> 뒤 <NUM>"
    )
    assert (
        redact_financial_numbers(
            "account-secret transaction-secret-one 정산",
            sensitive_identifiers={"account-secret", "transaction-secret-one"},
        )
        == "<ID> <ID> 정산"
    )


def test_redaction_removes_english_amounts_dates_and_embedded_identifiers() -> None:
    assert redact_financial_numbers("August card payment") == "<NUM> card payment"
    assert redact_financial_numbers("card bill fifty dollars") == "card bill <NUM>"
    assert redact_financial_numbers("ninety percent due next week") == "<NUM> due <NUM>"
    assert redact_financial_numbers("$fifty due next quarter") == "<NUM> due <NUM>"
    assert redact_financial_numbers("USD fifty") == "<NUM>"
    assert redact_financial_numbers("fifty USD") == "<NUM>"
    assert redact_financial_numbers("내일 결제 금요일 정산") == "<NUM> 결제 <NUM> 정산"
    assert redact_financial_numbers("내년 결제 이번 분기 정산") == "<NUM> 결제 <NUM> 정산"
    assert (
        redact_financial_numbers(
            "입금account-secret정산",
            sensitive_identifiers={"account-secret"},
        )
        == "입금<ID>정산"
    )


def test_short_identifiers_only_redact_as_standalone_tokens() -> None:
    identifiers = {"a", "t1"}

    assert redact_financial_numbers("Card payment", sensitive_identifiers=identifiers) == (
        "Card payment"
    )
    assert redact_financial_numbers("account a payment", sensitive_identifiers=identifiers) == (
        "account <ID> payment"
    )
    assert redact_financial_numbers("source t1 payment", sensitive_identifiers=identifiers) == (
        "source <ID> payment"
    )


def test_every_transaction_identifier_is_redacted_from_every_label() -> None:
    transactions = _transactions()
    transactions[0]["description"] = "transaction-secret-two linked-secret 정산"
    transactions[0]["linked_transaction_id"] = "linked-secret"

    inventory = build_label_inventory(transactions)

    assert all("transaction-secret-two" not in item.safe_text for item in inventory.items)
    assert all("linked-secret" not in item.safe_text for item in inventory.items)


def test_ai_group_transaction_order_does_not_depend_on_label_id_order() -> None:
    transactions = _transactions()
    transactions[0]["occurred_at"] = transactions[1]["occurred_at"]
    inventory = build_label_inventory(transactions)
    request = inventory.request_payload(import_id="import-opaque", locale="ko-KR")
    label_ids = [item["labelId"] for item in request["labels"]]

    def projected(order: list[str]) -> list[str]:
        response = {
            **{key: request[key] for key in request if key != "labels"},
            "groups": [
                {
                    "groupId": "group-card",
                    "labelIds": order,
                    "normalizedName": "카드사 자동이체",
                    "entityKind": "CARD_PAYMENT",
                    "categoryHint": "CARD_BILL",
                    "essentialHint": True,
                    "confidence": "HIGH",
                    "reason": "같은 카드 결제 항목입니다.",
                }
            ],
            "ungrouped": [],
        }
        group = project_validated_groups(inventory, response)[0]
        return [item["transaction_id"] for item in group.transactions]

    assert projected(label_ids) == projected(list(reversed(label_ids)))
