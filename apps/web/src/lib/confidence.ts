export function confidenceProgressCopy(value?: number) {
  if (typeof value !== "number") {
    return "확인 데이터가 쌓이면 분석 정밀도를 보여드려요";
  }
  if (value >= 0.8) return "확인 데이터가 충분히 쌓였어요";
  if (value >= 0.6) return "확인 데이터가 더 쌓이면 분석이 정밀해져요";
  return "예정 수입을 확인할수록 분석이 정밀해져요";
}
