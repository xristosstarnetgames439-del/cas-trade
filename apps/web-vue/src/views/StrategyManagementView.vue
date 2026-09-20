<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import dayjs from 'dayjs';
import {
  cancelStrategyRawDownload,
  createStrategy,
  createStrategyRawDownload,
  getStrategies,
  getStrategyRawDownload,
  getStrategyRun,
  runStrategy
} from '@/service/product-api';
import type {
  AuctionSnapshotItem,
  AuctionSnapshotResponse,
  BackgroundJobState,
  StrategyCreateRequest,
  StrategyDefinition,
  StrategyRawPeriod
} from '@/service/types';
import { useTradeDate } from '@/composables/useTradeDate';
import { formatWorkbenchNumber } from '@/components/common/workbench/workbench';

defineOptions({ name: 'StrategyManagementView' });

const router = useRouter();
const { tradeDate, setTradeDate } = useTradeDate();
const strategies = ref<StrategyDefinition[]>([]);
const activeStrategy = ref<StrategyDefinition | null>(null);
const result = ref<AuctionSnapshotResponse | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);
const createOpen = ref(false);
const creating = ref(false);
const selectedExactFilterSlots = ref<number[]>([]);
const storedRun = ref<AuctionSnapshotResponse | null>(null);
const downloadPeriod = ref<StrategyRawPeriod>('month');
const downloadJob = ref<BackgroundJobState | null>(null);
const downloadError = ref<string | null>(null);
const downloadWarningClosed = ref(false);
let downloadPollTimer: ReturnType<typeof setTimeout> | null = null;
const hasStoredRun = computed(() => storedRun.value !== null);
const downloadRunning = computed(() => downloadJob.value?.status === 'pending' || downloadJob.value?.status === 'running');
const downloadWarning = computed(() => {
  const jobResult = downloadJob.value?.result;
  if (!jobResult || typeof jobResult !== 'object') return null;
  const warning = (jobResult as Record<string, unknown>).warning;
  return typeof warning === 'string' ? warning : null;
});
const downloadProgress = computed(() => {
  if (downloadJob.value?.status === 'success') return 100;
  const current = downloadJob.value?.progress_current ?? 0;
  const total = downloadJob.value?.progress_total ?? 0;
  return total > 0 ? Math.round((current / total) * 100) : 0;
});
let probeSeq = 0;
const form = reactive<StrategyCreateRequest>({
  title: '',
  description: '',
  require_last_second_price_up: true,
  recent_limit_up_days: 3,
  min_open_gap_pct: -2,
  min_pattern_days: 0,
  min_board_count: 0,
  sort_by: 'days_boards'
});

const canCreate = computed(() => form.title.trim().length > 0 && form.description.trim().length > 0);
const exactFilterOptions = computed(() => {
  const configured = activeStrategy.value?.exact_conditions.data ?? [];
  return configured.length
    ? configured
    : Array.from({ length: 3 }, () => ({ label: '请在后端文件添加', isselect: false }));
});

async function loadStrategies() {
  try {
    strategies.value = (await getStrategies()).items;
    error.value = null;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '读取策略列表失败';
  }
}

async function execute() {
  if (!activeStrategy.value) return;
  const strategyId = activeStrategy.value.id;
  const date = tradeDate.value;
  loading.value = true;
  error.value = null;
  result.value = null;
  try {
    const snapshot = await runStrategy(strategyId, date, {
      limit: 100,
      exactConditions: selectedExactFilterSlots.value
    });
    if (activeStrategy.value?.id !== strategyId || tradeDate.value !== date) return;
    result.value = snapshot;
    storedRun.value = snapshot;
    probeSeq += 1;
  } catch (cause) {
    if (activeStrategy.value?.id !== strategyId || tradeDate.value !== date) return;
    result.value = null;
    error.value = cause instanceof Error ? cause.message : '执行策略失败';
  } finally {
    if (activeStrategy.value?.id === strategyId && tradeDate.value === date) {
      loading.value = false;
    }
  }
}

function viewStored() {
  if (!storedRun.value) return;
  error.value = null;
  result.value = storedRun.value;
}

async function probeStored() {
  const strategy = activeStrategy.value;
  if (!strategy) {
    storedRun.value = null;
    return;
  }
  probeSeq += 1;
  const seq = probeSeq;
  const date = tradeDate.value;
  try {
    const snapshot = await getStrategyRun(strategy.id, date);
    if (seq !== probeSeq) return;
    storedRun.value = snapshot;
  } catch {
    if (seq !== probeSeq) return;
    storedRun.value = null;
  }
}

function selectStrategy(item: StrategyDefinition) {
  activeStrategy.value = item;
  result.value = null;
  storedRun.value = null;
  error.value = null;
  resetExactFilter();
}

function backToStrategies() {
  activeStrategy.value = null;
  result.value = null;
  storedRun.value = null;
  error.value = null;
}

async function submitStrategy() {
  if (!canCreate.value) return;
  creating.value = true;
  error.value = null;
  try {
    const created = await createStrategy({ ...form, title: form.title.trim(), description: form.description.trim() });
    createOpen.value = false;
    resetForm();
    await loadStrategies();
    selectStrategy(created);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '新增策略失败';
  } finally {
    creating.value = false;
  }
}

function resetForm() {
  Object.assign(form, {
    title: '',
    description: '',
    require_last_second_price_up: true,
    recent_limit_up_days: 3,
    min_open_gap_pct: -2,
    min_pattern_days: 0,
    min_board_count: 0,
    sort_by: 'days_boards'
  });
}

function resetExactFilter() {
  selectedExactFilterSlots.value = exactFilterOptions.value.flatMap((condition, index) =>
    condition.isselect ? [index] : []
  );
}

function handleDateChange(value: string) {
  setTradeDate(value);
  result.value = null;
}

watch([tradeDate, activeStrategy], () => {
  probeStored();
});

function disableNonTradingDate(current: dayjs.Dayjs) {
  return current.day() === 0 || current.day() === 6;
}

function openStock(item: AuctionSnapshotItem) {
  router.push({
    path: `/stock/${encodeURIComponent(item.symbol)}`,
    query: { from: 'strategy-management', name: item.name || undefined, industry: item.industry || undefined }
  });
}

async function startDownload() {
  if (!activeStrategy.value || downloadRunning.value) return;
  const strategyId = activeStrategy.value.id;
  downloadError.value = null;
  downloadWarningClosed.value = false;
  try {
    downloadJob.value = await createStrategyRawDownload(strategyId, downloadPeriod.value);
    if (!isTerminalJob(downloadJob.value)) scheduleDownloadPoll(strategyId, downloadJob.value.job_id);
  } catch (cause) {
    downloadError.value = cause instanceof Error ? cause.message : '启动历史数据下载失败';
  }
}

function scheduleDownloadPoll(strategyId: string, jobId: string) {
  stopDownloadPoll();
  downloadPollTimer = setTimeout(() => {
    downloadPollTimer = null;
    pollDownload(strategyId, jobId);
  }, 1000);
}

async function pollDownload(strategyId: string, jobId: string) {
  try {
    const job = await getStrategyRawDownload(strategyId, jobId);
    if (downloadJob.value?.job_id !== jobId) return;
    downloadJob.value = job;
    if (!isTerminalJob(job)) scheduleDownloadPoll(strategyId, jobId);
    if (job.status === 'failed') downloadError.value = job.error || job.message;
  } catch (cause) {
    downloadError.value = cause instanceof Error ? cause.message : '读取历史数据下载进度失败';
  }
}

async function cancelDownload() {
  if (!activeStrategy.value || !downloadJob.value || !downloadRunning.value) return;
  try {
    downloadJob.value = await cancelStrategyRawDownload(activeStrategy.value.id, downloadJob.value.job_id);
  } catch (cause) {
    downloadError.value = cause instanceof Error ? cause.message : '取消历史数据下载失败';
  }
}

function isTerminalJob(job: BackgroundJobState) {
  return job.status === 'success' || job.status === 'failed' || job.status === 'canceled';
}

function stopDownloadPoll() {
  if (downloadPollTimer !== null) {
    clearTimeout(downloadPollTimer);
    downloadPollTimer = null;
  }
}

onMounted(loadStrategies);
onUnmounted(stopDownloadPoll);
</script>

<template>
  <div class="space-y-16px">
    <PageHeader
      title="策略管理"
      :description="activeStrategy ? `当前策略：${activeStrategy.title}` : '一个策略一个文件，点击策略进入详情'"
    >
      <a-date-picker
        :value="dayjs(tradeDate)"
        value-format="YYYY-MM-DD"
        :disabled-date="disableNonTradingDate"
        @change="(_, value) => handleDateChange(String(value))"
      />
      <a-select
        v-if="activeStrategy"
        v-model:value="downloadPeriod"
        class="download-period"
        :disabled="downloadRunning"
        :options="[
          { label: '本月', value: 'month' },
          { label: '近三月', value: 'three_months' },
          { label: '本年', value: 'year' }
        ]"
      />
      <a-button
        v-if="activeStrategy"
        data-testid="strategy-download-button"
        :loading="downloadRunning"
        @click="startDownload"
      >
        下载历史数据
      </a-button>
      <a-button v-if="!activeStrategy" type="primary" @click="createOpen = true">新增策略</a-button>
    </PageHeader>

    <a-alert v-if="error" :message="error" show-icon type="error" />

    <section v-if="!activeStrategy" class="strategy-grid">
      <article
        v-for="strategy in strategies"
        :key="strategy.id"
        class="strategy-card"
        role="button"
        tabindex="0"
        @click="selectStrategy(strategy)"
        @keydown.enter="selectStrategy(strategy)"
      >
        <div class="flex items-start justify-between gap-12px">
          <div>
            <h3 class="m-0 text-16px">{{ strategy.title }}</h3>
            <div class="mt-3px text-12px text-text-secondary">{{ strategy.path }}</div>
          </div>
          <a-tag color="green">可执行</a-tag>
        </div>
        <p class="mb-10px mt-10px text-13px text-text-secondary">{{ strategy.description }}</p>
        <div class="mt-12px text-13px text-primary font-600">点击查看并运行</div>
      </article>
    </section>

    <section v-else class="strategy-detail border border-border rounded-6px bg-container p-12px">
      <a-alert
        class="mb-10px"
        message="历史策略只读取本地文件；没有秒级竞价时不能计算有效抬价，系统不会自动放宽条件。"
        show-icon
        type="info"
      />
      <a-alert
        v-if="downloadError"
        class="download-notice mb-10px"
        :message="downloadError"
        closable
        show-icon
        type="error"
        @close="downloadError = null"
      />
      <a-alert
        v-if="downloadWarning && !downloadWarningClosed"
        class="download-notice mb-10px"
        :message="downloadWarning"
        closable
        show-icon
        type="warning"
        @close="downloadWarningClosed = true"
      />
      <div v-if="downloadJob" class="download-progress">
        <a-progress :percent="downloadProgress" size="small" />
        <span>{{ downloadJob.message }}</span>
        <a-button v-if="downloadRunning" size="small" danger @click="cancelDownload">取消</a-button>
      </div>
      <div class="strategy-detail-header">
        <a-button @click="backToStrategies">← 返回总列表</a-button>
        <h2>{{ activeStrategy.title }}</h2>
        <span class="strategy-source">来源 {{ activeStrategy.path }}</span>
        <span v-if="result?.generated_at" class="strategy-updated">
          更新 {{ dayjs(result.generated_at).format('HH:mm:ss') }}
        </span>
        <a-button data-testid="strategy-run-button" type="primary" :loading="loading" @click="execute">
          运行筛选
        </a-button>
        <a-button
          data-testid="strategy-view-button"
          :type="hasStoredRun ? 'primary' : 'default'"
          :disabled="!hasStoredRun || loading"
          @click="viewStored"
        >
          查看筛选
        </a-button>
      </div>

      <p class="mb-12px mt-8px text-13px text-text-secondary">{{ activeStrategy.description }}</p>

      <details class="strategy-disclosure">
        <summary>筛选条件（{{ activeStrategy.conditions.data.length }} 项）</summary>
        <ul class="condition-list">
          <li v-for="condition in activeStrategy.conditions.data" :key="condition">{{ condition }}</li>
        </ul>
      </details>

      <details class="strategy-disclosure">
        <summary>精确筛选（{{ selectedExactFilterSlots.length }}/{{ exactFilterOptions.length }}）</summary>
        <div class="exact-filter-scroll">
          <label
            v-for="(condition, index) in exactFilterOptions"
            :key="`${index}-${condition.label}`"
            class="exact-filter-option"
          >
            <input v-model="selectedExactFilterSlots" type="checkbox" :value="index" />
            <span>{{ condition.label }}</span>
          </label>
          <a-button
            v-if="selectedExactFilterSlots.length !== exactFilterOptions.length"
            size="small"
            @click="resetExactFilter"
          >
            全选
          </a-button>
        </div>
      </details>

      <div v-if="result" class="mb-8px mt-12px text-12px text-text-secondary">
        策略命中 {{ result.items.length }} 只
      </div>
      <DataList
        :items="result?.items ?? []"
        :loading="loading"
        :error="error"
        :empty-description="result ? '当前交易日没有符合条件的股票' : '点击运行筛选查看结果'"
      >
        <template #list-item="{ item }">
          <button class="result-row" type="button" @click="openStock(item as AuctionSnapshotItem)">
            <span class="font-600">{{ (item as AuctionSnapshotItem).name }} <small>{{ (item as AuctionSnapshotItem).symbol }}</small></span>
            <span>{{ (item as AuctionSnapshotItem).limit_up_pattern || '板型待确认' }}</span>
            <span :class="((item as AuctionSnapshotItem).open_gap_pct ?? 0) >= 0 ? 'text-error' : 'text-success'">
              竞价 {{ formatWorkbenchNumber((item as AuctionSnapshotItem).open_gap_pct, 'percent') }}
            </span>
            <span>抬价 {{ formatWorkbenchNumber((item as AuctionSnapshotItem).last_second_pct, 'percent') }}</span>
            <span>收盘 {{ formatWorkbenchNumber((item as AuctionSnapshotItem).close_price, 'price') }}</span>
            <span>
              有效抬价 {{ (item as AuctionSnapshotItem).valid_raise_count ?? '--' }} 次 · 量比
              {{ (item as AuctionSnapshotItem).auction_volume_ratio?.toFixed(2) ?? '--' }}
            </span>
          </button>
        </template>
      </DataList>
    </section>

    <a-modal v-model:open="createOpen" title="新增个人选股策略" :confirm-loading="creating" :ok-button-props="{ disabled: !canCreate }" @ok="submitStrategy">
      <a-form layout="vertical">
        <a-form-item label="策略标题" required><a-input v-model:value="form.title" :maxlength="40" /></a-form-item>
        <a-form-item label="策略简介" required><a-textarea v-model:value="form.description" :rows="3" :maxlength="300" /></a-form-item>
        <a-form-item label="最后一刻抬价"><a-switch v-model:checked="form.require_last_second_price_up" /></a-form-item>
        <div class="rule-grid">
          <a-form-item label="最近涨停窗口"><a-input-number v-model:value="form.recent_limit_up_days" :min="1" :max="20" addon-after="日" /></a-form-item>
          <a-form-item label="最低竞价涨幅"><a-input-number v-model:value="form.min_open_gap_pct" :min="-20" :max="20" :step="0.5" addon-after="%" /></a-form-item>
          <a-form-item label="至少几天"><a-input-number v-model:value="form.min_pattern_days" :min="0" :max="20" addon-after="天" /></a-form-item>
          <a-form-item label="至少几板"><a-input-number v-model:value="form.min_board_count" :min="0" :max="20" addon-after="板" /></a-form-item>
        </div>
        <a-form-item label="默认排序">
          <a-select
            v-model:value="form.sort_by"
            :options="[
              { label: '几天几板', value: 'days_boards' },
              { label: '竞价涨幅', value: 'open_gap' },
              { label: '最后抬价幅度', value: 'last_second_pct' }
            ]"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped>
.strategy-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
}

.download-period {
  width: 108px;
}

.download-progress {
  display: grid;
  grid-template-columns: minmax(180px, 320px) 1fr auto;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
  color: var(--text-color-2, #64748b);
  font-size: 12px;
}

.download-notice :deep(.ant-alert-message) {
  white-space: pre-line;
}

.strategy-card {
  padding: 14px;
  border: 1px solid var(--n-border-color, #e5e7eb);
  border-radius: 8px;
  background: var(--n-color, #fff);
  cursor: pointer;
}

.strategy-card:hover {
  border-color: #1677ff;
}

.strategy-detail {
  width: 100%;
  box-sizing: border-box;
}

.strategy-detail-header {
  display: flex;
  min-height: 36px;
  align-items: center;
  gap: 12px;
}

.strategy-detail-header h2 {
  flex: 0 0 auto;
  margin: 0;
  font-size: 16px;
}

.strategy-source {
  flex: 1 1 auto;
  overflow: hidden;
  min-width: 0;
  color: var(--text-color-2, #64748b);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.strategy-updated {
  margin-left: auto;
  color: var(--text-color-2, #64748b);
  font-size: 12px;
  white-space: nowrap;
}

.strategy-detail-header > :last-child {
  flex: 0 0 auto;
}

.strategy-disclosure {
  margin-top: 10px;
  border: 1px solid var(--n-border-color, #e5e7eb);
  border-radius: 6px;
}

.strategy-disclosure summary {
  padding: 10px 12px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
}

.strategy-disclosure[open] summary {
  border-bottom: 1px solid var(--n-border-color, #e5e7eb);
}

.condition-list {
  margin: 8px 0;
  padding-left: 18px;
  color: var(--text-color-2, #64748b);
  font-size: 12px;
  line-height: 1.8;
}

.exact-filter-scroll {
  display: flex;
  overflow-x: auto;
  align-items: center;
  gap: 12px;
  padding: 12px;
  white-space: nowrap;
}

.exact-filter-option {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--n-border-color, #e5e7eb);
  border-radius: 999px;
  color: var(--text-color-2, #64748b);
  cursor: pointer;
  font-size: 12px;
}

.exact-filter-option input {
  width: 14px;
  height: 14px;
  margin: 0;
  border-radius: 50%;
  accent-color: #1677ff;
}

.result-row {
  display: grid;
  grid-template-columns:
    minmax(180px, 1.2fr) minmax(90px, 0.5fr) minmax(110px, 0.6fr) minmax(100px, 0.6fr) minmax(90px, 0.5fr)
    minmax(160px, 0.8fr);
  width: 100%;
  gap: 12px;
  padding: 4px 0;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.rule-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 12px;
}

@media (max-width: 760px) {
  .strategy-detail-header {
    flex-wrap: wrap;
  }

  .strategy-updated {
    margin-left: 0;
  }

  .result-row,
  .rule-grid,
  .download-progress {
    grid-template-columns: 1fr;
  }
}
</style>
