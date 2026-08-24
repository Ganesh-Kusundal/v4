// Replay transport bar: host chrome for two replay modes.
// - Bar-replay (prefix-slice via ReplayController) for trader practice
//   (indicators recompute historically for free).
// - Tick-replay (SyntheticTickGenerator via /ws replay_*) for fill parity.
// This file owns only DOM; the controller lives in main.ts.
export interface ReplayBarApi {
  setState(state: { index: number; total: number; playing: boolean; speed: number; barTime: number | null }): void;
  setTickActive(active: boolean): void;
}

export function createReplayBar(container: HTMLElement, handlers: {
  onSeek(index: number): void;
  onPlay(): void;
  onPause(): void;
  onStop(): void;
  onSpeed(speed: number): void;
  onTickReplayToggle(): void;
  onTickSpeed(speed: number): void;
}): ReplayBarApi {
  container.innerHTML = "";

  const left = document.createElement("div");
  left.style.display = "flex";
  left.style.gap = "6px";
  left.style.alignItems = "center";

  const playBtn = document.createElement("button");
  playBtn.textContent = "Play";
  const stopBtn = document.createElement("button");
  stopBtn.textContent = "Stop";
  const speedSel = document.createElement("select");
  for (const s of [1, 2, 5, 10]) {
    const o = document.createElement("option");
    o.value = String(s);
    o.textContent = `${s}x`;
    speedSel.append(o);
  }

  const slider = document.createElement("input");
  slider.type = "range";
  slider.className = "rb-track";
  slider.min = "0";
  slider.max = "0";
  slider.value = "0";

  const clock = document.createElement("span");
  clock.className = "rb-clock";
  clock.textContent = "--";

  const tickBtn = document.createElement("button");
  tickBtn.textContent = "Sim ticks";
  tickBtn.title = "Tick-replay via SyntheticTickGenerator (fill parity)";
  const tickSpeed = document.createElement("select");
  for (const s of [5, 10, 20, 50]) {
    const o = document.createElement("option");
    o.value = String(s);
    o.textContent = `${s}x ticks`;
    tickSpeed.append(o);
  }

  left.append(playBtn, stopBtn, speedSel, tickBtn, tickSpeed);
  container.append(left, slider, clock);

  let playing = false;
  let _tickActive = false;

  function syncPlayLabel(): void {
    playBtn.textContent = playing ? "Pause" : "Play";
  }

  playBtn.addEventListener("click", () => {
    if (playing) handlers.onPause();
    else handlers.onPlay();
  });
  stopBtn.addEventListener("click", () => handlers.onStop());
  speedSel.addEventListener("change", () => handlers.onSpeed(Number(speedSel.value)));
  tickBtn.addEventListener("click", () => handlers.onTickReplayToggle());
  tickSpeed.addEventListener("change", () => handlers.onTickSpeed(Number(tickSpeed.value)));
  slider.addEventListener("input", () => handlers.onSeek(Number(slider.value)));

  return {
    setState(state) {
      playing = state.playing;
      syncPlayLabel();
      slider.max = String(Math.max(0, state.total - 1));
      slider.value = String(state.index);
      speedSel.value = String(state.speed);
      if (state.barTime !== null) {
        const d = new Date(state.barTime * 1000);
        clock.textContent = d.toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false });
      } else {
        clock.textContent = "--";
      }
      container.classList.add("active");
    },
    setTickActive(active) {
      _tickActive = active;
      tickBtn.textContent = active ? "Stop ticks" : "Sim ticks";
      tickBtn.classList.toggle("primary", active);
      if (active) container.classList.add("active");
      void _tickActive;
    },
  };
}
