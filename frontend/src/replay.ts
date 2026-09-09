import type { Widget } from 'openalgo-charts/widget';
import { barSocket, mapWsInterval } from './feed';

/**
 * Replay transport bar, docked as the widget root's last grid row (below the
 * status line). One shared BarSocket connection: replay commands are plain
 * sends, acks come back as typed frames. Click-to-pick a start bar is skipped
 * for now: chart.subscribeClick only yields hit-tested primitive ids, not
 * bar times, so picking would need its own pointer plumbing (replay_start
 * uses the backend's default trailing window instead).
 */
export function mountReplayBar(widget: Widget): void {
  const doc = widget.root.ownerDocument;
  const bar = doc.createElement('div');
  bar.className = 'v4-replaybar';
  bar.style.cssText =
    'display:flex;align-items:center;gap:8px;padding:6px 12px;border-top:1px solid var(--oac-bd);';

  const button = (label: string): HTMLButtonElement => {
    const b = doc.createElement('button');
    b.className = 'oac-btn';
    b.textContent = label;
    return b;
  };
  const start = button('Start');
  const pause = button('Pause');
  const resume = button('Resume');
  const stop = button('Stop');
  const speed = doc.createElement('select');
  for (const s of ['0.5', '1', '2', '5', '10']) {
    const opt = doc.createElement('option');
    opt.value = s;
    opt.textContent = `${s}x`;
    speed.appendChild(opt);
  }
  speed.value = '1';
  bar.append(start, pause, resume, speed, stop);
  widget.root.appendChild(bar);

  const setRunning = (running: boolean): void => {
    start.disabled = running;
    pause.disabled = resume.disabled = stop.disabled = !running;
  };
  setRunning(false);

  // Scopes error toasts to actual replay sessions (M-2): the shared socket
  // delivers every server error frame, most of which are not replay's business.
  let replaying = false;

  const frameMessage = (msg: unknown): string => {
    const m = msg as { message?: unknown };
    return typeof m.message === 'string' ? m.message : 'unknown error';
  };
  barSocket.on('replay_started', () => {
    replaying = true;
    setRunning(true);
    widget.context.toast('Replay started');
  });
  barSocket.on('replay_paused', () => widget.context.toast('Replay paused'));
  barSocket.on('replay_resumed', () => widget.context.toast('Replay resumed'));
  barSocket.on('replay_done', () => {
    replaying = false;
    setRunning(false);
    widget.context.toast('Replay finished');
  });
  barSocket.on('replay_stopped', () => {
    replaying = false;
    setRunning(false);
    widget.context.toast('Replay stopped');
  });
  barSocket.on('error', (msg) => {
    const message = frameMessage(msg);
    if (replaying || /replay/i.test(message)) {
      widget.context.toast(`Replay: ${message}`, 'error');
    } else {
      console.warn('replay bar: ignoring non-replay error frame', message);
    }
  });
  // Connection dropped mid-replay: the server's replay state died with the
  // socket. Clean it up defensively and reset the UI; we deliberately do NOT
  // auto-restart replay on reconnect — the client's bar state has moved on
  // and a blind replay_start would desync the chart.
  barSocket.onOpen(() => {
    if (!replaying) return;
    replaying = false;
    setRunning(false);
    barSocket.send({ type: 'replay_stop' });
    widget.context.toast('Replay aborted: connection lost');
  });

  start.addEventListener('click', () => {
    barSocket.send({
      type: 'replay_start',
      instrument: `${widget.exchange()}:${widget.symbol()}`,
      interval: mapWsInterval(widget.interval()),
      speed: Number(speed.value),
    });
  });
  pause.addEventListener('click', () => barSocket.send({ type: 'replay_pause' }));
  resume.addEventListener('click', () => barSocket.send({ type: 'replay_resume' }));
  stop.addEventListener('click', () => barSocket.send({ type: 'replay_stop' }));
  speed.addEventListener('change', () =>
    barSocket.send({ type: 'replay_speed', speed: Number(speed.value) }),
  );
  // # ponytail: teardown — the host never calls widget.destroy (page-lifetime app),
  // so these barSocket handlers are never released; wire them up if a destroy hook appears.
}
