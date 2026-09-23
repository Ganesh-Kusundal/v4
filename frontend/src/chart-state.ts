// Shared mutable chart state — single source of truth for main.ts and sub-modules.

export interface RawBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

export interface BookOrder {
  id: string;
  symbol: string;
  exchange: string;
  side: string;
  type: string;
  qty: number;
  filled_qty?: number;
  filledQty?: number;
  price: number;
  trigger_price?: number | null;
  triggerPrice?: number | null;
  status: string;
}

export interface BookPosition {
  symbol: string;
  exchange: string;
  net_qty?: number;
  netQty?: number;
  avg_price?: number;
  avgPrice?: number;
  unrealized_pnl?: number;
  product?: string;
}

// Chart handles
let _chart: any = null;
let _price: any = null;
let _volume: any = null;
let _ltpLine: any = null;

export const chart = new Proxy({} as any, {
  get(_, p) { return _chart?.[p]; },
  set(_, p, v) { _chart = v; return true; },
});
export const setChart = (v: any) => { _chart = v; };
export const price = new Proxy({} as any, {
  get(_, p) { return _price?.[p]; },
  set(_, p, v) { _price = v; return true; },
});
export const setPrice = (v: any) => { _price = v; };
export const volume = new Proxy({} as any, {
  get(_, p) { return _volume?.[p]; },
  set(_, p, v) { _volume = v; return true; },
});
export const setVolume = (v: any) => { _volume = v; };
export const ltpLine = new Proxy({} as any, {
  get(_, p) { return _ltpLine?.[p]; },
  set(_, p, v) { _ltpLine = v; return true; },
});
export const setLtpLine = (v: any) => { _ltpLine = v; };

// Bar data
export let tickN = 0;
export let rawBars: RawBar[] = [];
export let builder: any = null;
export let liveAgg = false;
export let lastLtp: number | null = null;
export let ctxPrice = 0;
export let lastReq: { symbol: string; exchange: string; interval: string } | null = null;
export const setTickN = (v: number) => { tickN = v; };
export const setRawBars = (v: RawBar[]) => { rawBars = v; };
export const setBuilder = (v: any) => { builder = v; };
export const setLiveAgg = (v: boolean) => { liveAgg = v; };
export const setLastLtp = (v: number | null) => { lastLtp = v; };
export const setCtxPrice = (v: number) => { ctxPrice = v; };
export const setLastReq = (v: typeof lastReq) => { lastReq = v; };

// Replay
export let isReplaying = false;
export let replayPicking = false;
export let replayPickIndex: number | null = null;
export let replayShade: any = null;
export let replayFullBars: RawBar[] | null = null;
export let replayTotalBars = 0;
export let replayCurrentIndex = 0;
export let replayIsPlaying = false;
export let replaySpeed = 1;
export const REPLAY_SPEEDS = [0.5, 1, 2, 5, 10];
export const setIsReplaying = (v: boolean) => { isReplaying = v; };
export const setReplayPicking = (v: boolean) => { replayPicking = v; };
export const setReplayPickIndex = (v: number | null) => { replayPickIndex = v; };
export const setReplayShade = (v: any) => { replayShade = v; };
export const setReplayFullBars = (v: RawBar[] | null) => { replayFullBars = v; };
export const setReplayTotalBars = (v: number) => { replayTotalBars = v; };
export const setReplayCurrentIndex = (v: number) => { replayCurrentIndex = v; };
export const setReplayIsPlaying = (v: boolean) => { replayIsPlaying = v; };
export const setReplaySpeed = (v: number) => { replaySpeed = v; };

// Book / trading
export let bookTimer: any = null;
export const orderLines = new Map<string, { line: any; order: BookOrder; dragFrom?: number | null }>();
export let posLine: any = null;
export let position: { net: number; avg: number; product?: string } | null = null;
export const setBookTimer = (v: any) => { bookTimer = v; };
export const setPosLine = (v: any) => { posLine = v; };
export const setPosition = (v: typeof position) => { position = v; };

// WS unsubs
export let unsubBar: (() => void) | null = null;
export let unsubDepth: (() => void) | null = null;
export let unsubQuote: (() => void) | null = null;
export let unsubOrder: (() => void) | null = null;
export let unsubFill: (() => void) | null = null;
export let unsubPos: (() => void) | null = null;
export let unsubOpen: (() => void) | null = null;
export let unsubClose: (() => void) | null = null;
export const setUnsubBar = (v: typeof unsubBar) => { unsubBar = v; };
export const setUnsubDepth = (v: typeof unsubDepth) => { unsubDepth = v; };
export const setUnsubQuote = (v: typeof unsubQuote) => { unsubQuote = v; };
export const setUnsubOrder = (v: typeof unsubOrder) => { unsubOrder = v; };
export const setUnsubFill = (v: typeof unsubFill) => { unsubFill = v; };
export const setUnsubPos = (v: typeof unsubPos) => { unsubPos = v; };
export const setUnsubOpen = (v: typeof unsubOpen) => { unsubOpen = v; };
export const setUnsubClose = (v: typeof unsubClose) => { unsubClose = v; };
