export function exactConditionsKey(selected: number[]): string {
  return [...selected].sort((left, right) => left - right).join(',');
}
