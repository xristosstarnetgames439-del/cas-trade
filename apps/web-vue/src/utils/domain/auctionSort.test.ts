import { describe, expect, it } from 'vitest';
import type { AuctionSnapshotItem } from '@/service/types';
import { AUCTION_SORT_OPTIONS, sortAuctionItems } from './auctionSort';

function item(symbol: string, overrides: Partial<AuctionSnapshotItem> = {}): AuctionSnapshotItem {
  return {
    symbol,
    name: symbol,
    industry: null,
    themes: [],
    hot_theme_rank: null,
    hot_theme_score: null,
    theme_auction_rank: null,
    theme_resonance: false,
    last_price: null,
    current_pct_change: null,
    open_gap_pct: null,
    turnover_rate: null,
    turnover_cny: null,
    volume: null,
    auction_score: 0,
    tier: 'neutral',
    action_note: null,
    signals: [],
    risk_flags: [],
    quote_time: null,
    last_second_price_up: null,
    last_second_pct: null,
    prev_node_price: null,
    prev_node_time: null,
    limit_up_3d: null,
    ...overrides
  };
}

describe('auctionSort snatch strategy', () => {
  it('exposes the 竞价抢筹 strategy option', () => {
    const snatch = AUCTION_SORT_OPTIONS.find(option => option.value === 'snatch');
    expect(snatch?.label).toBe('竞价抢筹');
  });

  it('keeps only stocks matching both signals and ranks by days then boards', () => {
    const items = [
      item('A', { last_second_price_up: true, last_second_pct: 0.5, limit_up_3d: true, open_gap_pct: 1, limit_up_pattern_days: 2, limit_up_board_count: 2, auction_score: 60 }),
      item('B', { last_second_price_up: true, last_second_pct: 2.0, limit_up_3d: true, open_gap_pct: 1, limit_up_pattern_days: 3, limit_up_board_count: 1, auction_score: 90 }),
      item('C', { last_second_price_up: true, last_second_pct: 1.0, limit_up_3d: false, open_gap_pct: 1, auction_score: 99 })
    ];
    const result = sortAuctionItems(items, 'snatch');
    expect(result.map(entry => entry.symbol)).toEqual(['B', 'A']);
  });

  it('removes non-qualified stocks regardless of score', () => {
    const items = [
      item('high-score-none', { auction_score: 100 }),
      item('both', { last_second_price_up: true, last_second_pct: 1.0, limit_up_3d: true, open_gap_pct: 0, auction_score: 10 })
    ];
    const result = sortAuctionItems(items, 'snatch');
    expect(result.map(entry => entry.symbol)).toEqual(['both']);
  });

  it('removes partial and missing signals', () => {
    const items = [
      item('none', { auction_score: 99 }),
      item('limit-up-only', { limit_up_3d: true, auction_score: 30 }),
      item('price-up-only', { last_second_price_up: true, last_second_pct: 0.3, auction_score: 20 })
    ];
    const result = sortAuctionItems(items, 'snatch');
    expect(result).toEqual([]);
  });

  it('excludes opens below minus two and supports open-gap sorting', () => {
    const items = [
      item('excluded', { last_second_price_up: true, limit_up_3d: true, open_gap_pct: -2.01 }),
      item('low', { last_second_price_up: true, limit_up_3d: true, open_gap_pct: -2 }),
      item('high', { last_second_price_up: true, limit_up_3d: true, open_gap_pct: 3 })
    ];

    expect(sortAuctionItems(items, 'snatch', 'open_gap').map(entry => entry.symbol)).toEqual(['high', 'low']);
  });

  it('uses open gap after equal days and boards', () => {
    const items = [
      item('negative', { last_second_price_up: true, limit_up_3d: true, open_gap_pct: -1.99, limit_up_pattern_days: 8, limit_up_board_count: 5 }),
      item('zero', { last_second_price_up: true, limit_up_3d: true, open_gap_pct: 0, limit_up_pattern_days: 8, limit_up_board_count: 5 })
    ];

    expect(sortAuctionItems(items, 'snatch').map(entry => entry.symbol)).toEqual(['zero', 'negative']);
  });
});
