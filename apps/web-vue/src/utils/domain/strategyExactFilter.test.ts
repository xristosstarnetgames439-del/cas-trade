import { describe, expect, it } from 'vitest';
import { exactConditionsKey } from './strategyExactFilter';

describe('strategyExactFilter', () => {
  it('keeps the same key for the same slots in any order', () => {
    expect(exactConditionsKey([3, 0, 1])).toBe('0,1,3');
  });
});
