import type { Bar } from 'openalgo-charts';
import type { Widget } from 'openalgo-charts/widget';
import { barSocket, mapWsInterval } from './feed';
import type { Dock } from './dock';
import { createPanel } from './dock';

export type ReplayState = boolean;
export type ReplayStateChange = (state: ReplayState) => void;

export interface ReplayBarOptions {
  onStateChange?: ReplayStateChange;
}

const record = (value: unknown): Record<string, unknown> | null =>
  typeof value === 'object' && value !== null ? value as Record<string, unknown> : null;

const finiteNumber = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;

const nonNegativeInteger = (value: unknown): number | null => {
  const parsed = finiteNumber(value);
  return parsed !== null && Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
};

const runId = (value: unknown): string | null => {
  const message = record(value);
  const id = message?.run_id;
  return typeof id === 'string' && id.length > 0 ? id : null;
};

const frameMessage = (value: unknown): string => {
  const message = record(value)?.message;
  return typeof message === 'string' ? message : 'unknown error';
};

const wireBars = (value: unknown): Bar[] | null => {
  if (!Array.isArray(value)) return null;
  const bars: Bar[] = [];
  for (const candidate of value) {
    const bar = record(candidate);
    if (bar === null) return null;
    const time = finiteNumber(bar.time);
    const open = finiteNumber(bar.open);
    const high = finiteNumber(bar.high);
    const low = finiteNumber(bar.low);
    const close = finiteNumber(bar.close);
    if (time === null || open === null || high === null || low === null || close === null) return null;
    const next: Bar = { time, open, high, low, close };
    const volume = finiteNumber(bar.volume);
    if (volume !== null) next.volume = volume;
    bars.push(next);
  }
  return bars;
};

export function mountReplayBar(
  widget: Widget,
  dock: Dock,
  options: ReplayBarOptions | ReplayStateChange = {},
): void {
  const doc = dock.ownerDocument;
  const onStateChange = typeof options === 'function' ? options : options.onStateChange;
  const panel = createPanel(dock, { title: 'Replay', hint: 'Market replay transport' });
  panel.body.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 12px;flex-wrap:wrap;';

  const button = (label: string): HTMLButtonElement => {
    const item = doc.createElement('button');
    item.className = 'oac-btn';
    item.textContent = label;
    return item;
  };
  const start = button('Start');
  start.id = 'replay-start';
  const pause = button('Pause');
  pause.id = 'replay-pause';
  const resume = button('Resume');
  resume.id = 'replay-resume';
  const step = button('Step');
  step.id = 'replay-step';
  const stop = button('Stop');
  stop.id = 'replay-stop';
  const speed = doc.createElement('select');
  speed.id = 'replay-speed';
  for (const value of ['0.5', '1', '2', '5', '10']) {
    const option = doc.createElement('option');
    option.value = value;
    option.textContent = `${value}x`;
    speed.appendChild(option);
  }
  speed.value = '1';

  const scrub = doc.createElement('input');
  scrub.id = 'replay-scrub';
  scrub.type = 'range';
  scrub.min = '0';
  scrub.max = '0';
  scrub.value = '0';
  scrub.disabled = true;
  scrub.title = 'Seek to bar';
  scrub.style.cssText = 'flex:1;min-width:120px;';

  const count = doc.createElement('span');
  count.id = 'replay-count';
  count.style.cssText = 'font-size:11px;color:var(--mut);white-space:nowrap;';
  count.textContent = '0 / 0';

  const pace = doc.createElement('span');
  pace.id = 'replay-pace';
  pace.style.cssText = 'font-size:11px;color:var(--mut);white-space:nowrap;';
  pace.textContent = '';

  panel.body.append(start, pause, resume, step, speed, scrub, count, pace, stop);

  let replaying = false;
  let activeRunId: string | null = null;
  let totalBars = 0;
  let currentIndex = 0;
  let seekPending = false;

  const publish = (value: boolean): void => {
    if (replaying === value) return;
    replaying = value;
    onStateChange?.(value);
  };
  const setRunning = (running: boolean): void => {
    start.disabled = running;
    pause.disabled = resume.disabled = step.disabled = stop.disabled = !running;
    speed.disabled = !running;
    scrub.disabled = !running || totalBars <= 0;
  };
  const setCursor = (index: number): void => {
    if (totalBars <= 0) return;
    currentIndex = Math.max(0, Math.min(totalBars - 1, Math.trunc(index)));
    scrub.value = String(currentIndex);
    count.textContent = `${currentIndex + 1} / ${totalBars}`;
  };
  const resetUi = (): void => {
    totalBars = 0;
    currentIndex = 0;
    scrub.max = '0';
    scrub.value = '0';
    count.textContent = '0 / 0';
    pace.textContent = '';
    setRunning(false);
  };
  const exitChart = (): void => {
    activeRunId = null;
    try {
      widget.exitReplay();
    } catch (error) {
      console.warn('replay bar: chart restore failed', error);
    }
  };
  const cleanup = (): void => {
    if (activeRunId !== null) exitChart();
    seekPending = false;
    publish(false);
    resetUi();
  };
  const replayError = (message: string): void => {
    if (activeRunId !== null) barSocket.send({ type: 'replay_stop' });
    cleanup();
    panel.setError(`Replay: ${message}`);
    widget.context.toast(`Replay: ${message}`, 'error');
  };
  const isCurrentFrame = (msg: unknown): boolean =>
    activeRunId !== null && runId(msg) === activeRunId;
  const finish = (msg: unknown): boolean => {
    if (activeRunId === null) {
      if (!replaying) return false;
    } else if (!isCurrentFrame(msg)) {
      return false;
    }
    cleanup();
    return true;
  };

  setRunning(false);

  barSocket.on('bar', (bar, meta) => {
    if (activeRunId === null || meta?.source !== 'sim' || meta.run_id !== activeRunId) return;
    seekPending = false;
    widget.pushReplayBar(bar, meta);
  });
  barSocket.on('replay_loading', (msg) => {
    if (activeRunId === null && !replaying) return;
    if (activeRunId !== null && !isCurrentFrame(msg)) return;
    seekPending = false;
    publish(true);
    setRunning(true);
  });
  barSocket.on('replay_started', (msg) => {
    const message = record(msg);
    const nextRunId = runId(msg);
    const nextTotalBars = nonNegativeInteger(message?.total_bars);
    if (activeRunId === null && !replaying) return;
    if (activeRunId !== null && nextRunId === null) return;
    if (activeRunId !== null && nextRunId !== activeRunId && !seekPending) return;
    if (message === null || nextRunId === null || nextTotalBars === null) {
      replayError('invalid replay_started frame');
      return;
    }
    if (activeRunId !== null && activeRunId !== nextRunId) {
      if (!seekPending) return;
      exitChart();
    }
    seekPending = false;
    const suppliedBars = message.bars === undefined ? null : wireBars(message.bars);
    if (message.bars !== undefined && suppliedBars === null) {
      replayError('invalid replay bars');
      return;
    }
    const startTime = finiteNumber(message.start_time);
    const initialBars = suppliedBars ?? widget.series.getData().filter((bar) =>
      startTime === null || bar.time <= startTime,
    );
    try {
      if (!widget.enterReplay(nextRunId, initialBars)) throw new Error('chart replay lifecycle unavailable');
    } catch (error) {
      replayError(error instanceof Error ? error.message : String(error));
      return;
    }
    activeRunId = nextRunId;
    totalBars = nextTotalBars;
    scrub.max = String(Math.max(0, totalBars - 1));
    setCursor(0);
    setRunning(true);
    publish(true);
    panel.setError(null);
    const wallSeconds = finiteNumber(message.wall_seconds_per_bar);
    pace.textContent = wallSeconds !== null
      ? `${wallSeconds}s / bar (simulated)`
      : 'simulated';
    widget.context.toast('Replay started');
  });
  barSocket.on('replay_paused', (msg) => {
    if (!isCurrentFrame(msg)) return;
    seekPending = false;
    widget.context.toast('Replay paused');
  });
  barSocket.on('replay_resumed', (msg) => {
    if (!isCurrentFrame(msg)) return;
    seekPending = false;
    widget.context.toast('Replay resumed');
  });
  barSocket.on('replay_stepped', (msg) => {
    if (!isCurrentFrame(msg)) return;
    seekPending = false;
    const message = record(msg);
    const index = nonNegativeInteger(message?.bar_index ?? message?.index);
    setCursor(index ?? currentIndex + 1);
    widget.context.toast('Replay stepped');
  });
  const applySeek = (msg: unknown): void => {
    if (!isCurrentFrame(msg)) return;
    seekPending = false;
    const message = record(msg);
    if (message === null) return;
    const index = nonNegativeInteger(message.bar_index ?? message.index);
    if (index !== null) setCursor(index);
    if (message.bars === undefined) return;
    const bars = wireBars(message.bars);
    if (bars === null || !widget.replaceReplayBars(bars)) replayError('invalid replay seek bars');
  };
  barSocket.on('replay_seeked', applySeek);
  barSocket.on('replay_seek', applySeek);
  barSocket.on('replay_done', (msg) => {
    if (finish(msg)) widget.context.toast('Replay finished');
  });
  barSocket.on('replay_stopped', (msg) => {
    if (finish(msg)) widget.context.toast('Replay stopped');
  });
  barSocket.on('replay_error', (msg) => {
    if (seekPending) {
      replayError(frameMessage(msg));
      return;
    }
    if (activeRunId === null) {
      if (replaying) replayError(frameMessage(msg));
    } else if (isCurrentFrame(msg)) {
      replayError(frameMessage(msg));
    }
  });
  barSocket.on('error', (msg) => {
    const message = frameMessage(msg);
    if (activeRunId === null) {
      if (replaying) {
        replayError(message);
      } else if (/replay/i.test(message)) {
        panel.setError(`Replay: ${message}`);
        widget.context.toast(`Replay: ${message}`, 'error');
      } else {
        console.warn('replay bar: ignoring non-replay error frame', message);
      }
    } else if (isCurrentFrame(msg)) {
      replayError(message);
    }
  });
  barSocket.onClose(() => {
    if (!replaying && activeRunId === null) return;
    cleanup();
    panel.setError('Replay aborted: connection lost');
    widget.context.toast('Replay aborted: connection lost', 'error');
  });
  const stopForContextChange = (): void => {
    if (!replaying && activeRunId === null) return;
    barSocket.send({ type: 'replay_stop' });
    cleanup();
    panel.setError('Replay stopped: symbol or interval changed');
    widget.context.toast('Replay stopped: symbol or interval changed');
  };
  widget.on('symbol', stopForContextChange);
  widget.on('interval', stopForContextChange);

  start.addEventListener('click', () => {
    if (replaying) return;
    seekPending = false;
    panel.setError(null);
    publish(true);
    setRunning(true);
    barSocket.send({
      type: 'replay_start',
      instrument: `${widget.exchange()}:${widget.symbol()}`,
      interval: mapWsInterval(widget.interval()),
      speed: Number(speed.value),
      ticks_per_bar: 10,
    });
  });
  pause.addEventListener('click', () => barSocket.send({ type: 'replay_pause' }));
  resume.addEventListener('click', () => barSocket.send({ type: 'replay_resume' }));
  step.addEventListener('click', () => barSocket.send({ type: 'replay_step' }));
  stop.addEventListener('click', () => barSocket.send({ type: 'replay_stop' }));
  speed.addEventListener('change', () =>
    barSocket.send({ type: 'replay_speed', speed: Number(speed.value) }),
  );
  scrub.addEventListener('change', () => {
    if (!replaying || totalBars <= 0) return;
    const index = nonNegativeInteger(Number(scrub.value));
    if (index === null) return;
    setCursor(index);
    seekPending = true;
    barSocket.send({ type: 'replay_seek', index });
  });
}
