// @vitest-environment jsdom

import { defineComponent, ref } from 'vue';
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuctionSnapshotResponse, StrategyDefinition } from '@/service/types';
import StrategyManagementView from './StrategyManagementView.vue';

const api = vi.hoisted(() => ({
  createStrategy: vi.fn(),
  getStrategies: vi.fn(),
  getStrategyRun: vi.fn(),
  runStrategy: vi.fn()
}));
const tradeDateState = vi.hoisted(() => ({ value: '2026-09-18' }));

vi.mock('@/service/product-api', () => api);
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('@/composables/useTradeDate', () => ({
  useTradeDate: () => {
    const tradeDate = ref(tradeDateState.value);
    return {
      tradeDate,
      setTradeDate: (value: string) => {
        tradeDate.value = value;
        tradeDateState.value = value;
      }
    };
  }
}));

const STRATEGY: StrategyDefinition = {
  id: 'auction_snatch',
  title: '竞价抢筹',
  path: 'app/strategies/auction_snatch.py',
  description: '集合竞价抬价',
  conditions: { data: ['09:25 抬价'] },
  exact_conditions: {
    data: [
      { label: '至少 3 次有效抬价', isselect: true },
      { label: '断板最多 1 日', isselect: true },
      { label: '量比大于 1', isselect: false },
      { label: '量比不低于 60%', isselect: true }
    ]
  },
  status: 'active',
  version: 5
};

const SNAPSHOT: AuctionSnapshotResponse = {
  trade_date: '2026-09-18',
  session: 'closed',
  snapshot_status: 'cached',
  cache_age_seconds: null,
  metrics: {
    candidate_count: 1,
    strong_high_open_count: 0,
    high_risk_count: 0,
    total_turnover_cny: 0
  },
  items: [
    {
      symbol: '000993.SZ',
      name: '闽东电力',
      industry: '电力',
      themes: [],
      hot_theme_rank: null,
      hot_theme_score: null,
      theme_auction_rank: null,
      theme_resonance: false,
      last_price: 18.3,
      current_pct_change: 1.84,
      open_gap_pct: 1.84,
      turnover_rate: null,
      turnover_cny: 1000,
      volume: 100,
      auction_score: 2.81,
      tier: 'neutral',
      action_note: null,
      signals: [],
      risk_flags: [],
      quote_time: '09:25:00',
      last_second_price_up: true,
      last_second_pct: 2.81,
      prev_node_price: 17.8,
      prev_node_time: '2026-09-18T09:24:57',
      close_price: 18.3
    }
  ],
  source_status: [],
  generated_at: '2026-09-18T15:05:00+08:00'
};

const ButtonStub = defineComponent({
  name: 'AButton',
  props: ['loading', 'disabled', 'type'],
  emits: ['click'],
  template: '<button :disabled="disabled" :data-type="type" @click="$emit(\'click\')"><slot /></button>'
});

const DataListStub = defineComponent({
  name: 'DataList',
  props: ['items', 'loading', 'error', 'emptyDescription'],
  template: `
    <div>
      <p v-if="!items?.length">{{ emptyDescription }}</p>
      <div v-for="item in items" :key="item.symbol">
        <slot name="list-item" :item="item" />
      </div>
    </div>
  `
});

function mountView() {
  return mount(StrategyManagementView, {
    global: {
      stubs: {
        AAlert: { props: ['message'], template: '<div role="alert">{{ message }}</div>' },
        AButton: ButtonStub,
        ADatePicker: true,
        AForm: true,
        AFormItem: true,
        AInput: true,
        AInputNumber: true,
        AModal: true,
        ASelect: true,
        ASwitch: true,
        ATag: true,
        ATextarea: true,
        DataList: DataListStub,
        PageHeader: defineComponent({
          props: ['title', 'description'],
          template: '<header><slot /></header>'
        })
      }
    }
  });
}

describe('StrategyManagementView', () => {
  afterEach(() => {
    vi.clearAllMocks();
    tradeDateState.value = '2026-09-18';
  });

  beforeEach(() => {
    api.getStrategies.mockResolvedValue({ items: [STRATEGY] });
    api.getStrategyRun.mockResolvedValue(null);
  });

  it('lights the view button only after a stored run exists and renders close price', async () => {
    const wrapper = mountView();
    await flushPromises();
    await wrapper.get('.strategy-card').trigger('click');
    await flushPromises();

    const viewButton = wrapper.get('[data-testid="strategy-view-button"]');
    expect(viewButton.attributes('disabled')).toBeDefined();
    expect(api.getStrategyRun).toHaveBeenCalledWith('auction_snatch', '2026-09-18');

    api.runStrategy.mockResolvedValue(SNAPSHOT);
    await wrapper.get('[data-testid="strategy-run-button"]').trigger('click');
    await flushPromises();

    expect(viewButton.attributes('disabled')).toBeUndefined();
    expect(viewButton.attributes('data-type')).toBe('primary');
    expect(wrapper.text()).toContain('闽东电力');
    expect(wrapper.text()).toContain('收盘 18.30');
  });

  it('reads the stored table without rerunning when view is clicked', async () => {
    api.getStrategyRun.mockResolvedValue(SNAPSHOT);
    const wrapper = mountView();
    await flushPromises();
    await wrapper.get('.strategy-card').trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('点击运行筛选查看结果');
    await wrapper.get('[data-testid="strategy-view-button"]').trigger('click');

    expect(api.runStrategy).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain('闽东电力');
    expect(wrapper.text()).toContain('收盘 18.30');
  });

  it('clears stale stored rows while a live run is in flight', async () => {
    api.getStrategyRun.mockResolvedValue(SNAPSHOT);
    let finishRun: ((value: AuctionSnapshotResponse) => void) | undefined;
    api.runStrategy.mockImplementation(
      () =>
        new Promise<AuctionSnapshotResponse>(resolve => {
          finishRun = resolve;
        })
    );
    const wrapper = mountView();
    await flushPromises();
    await wrapper.get('.strategy-card').trigger('click');
    await flushPromises();
    await wrapper.get('[data-testid="strategy-view-button"]').trigger('click');
    expect(wrapper.text()).toContain('闽东电力');

    await wrapper.get('[data-testid="strategy-run-button"]').trigger('click');
    await flushPromises();
    expect(wrapper.text()).not.toContain('闽东电力');
    expect(wrapper.get('[data-testid="strategy-view-button"]').attributes('disabled')).toBeDefined();

    finishRun?.(SNAPSHOT);
    await flushPromises();
    expect(wrapper.text()).toContain('闽东电力');
  });
});
