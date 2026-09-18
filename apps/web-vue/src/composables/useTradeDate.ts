import { ref } from 'vue';
import { getAuctionCacheTradeDate } from '@/utils/domain/marketOverview';

export function useTradeDate(initialDate?: string) {
  const tradeDate = ref(initialDate || getAuctionCacheTradeDate());

  function setTradeDate(value: string) {
    const next = value.trim();
    if (next) tradeDate.value = next;
  }

  return { tradeDate, setTradeDate };
}
