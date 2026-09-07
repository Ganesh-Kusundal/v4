// Generic overlay host — watchlist, orders, strategies, draw rail.
export interface Overlay {
  open(): void;
  close(): void;
  toggle(): void;
  isOpen(): boolean;
}

export function createOverlay(config: {
  position: 'right' | 'left' | 'bottom';
  width?: number;
  height?: number;
  content: HTMLElement;
}): Overlay {
  let isOpen = false;

  const el = document.createElement('div');
  el.className = `overlay overlay--${config.position}`;
  if (config.width) el.style.width = `${config.width}px`;
  if (config.height) el.style.height = `${config.height}px`;
  el.append(config.content);
  el.style.display = 'none';

  function open(): void {
    el.style.display = 'flex';
    // Force reflow for transition
    el.getBoundingClientRect();
    el.classList.add('open');
    isOpen = true;
  }

  function close(): void {
    el.classList.remove('open');
    const onEnd = () => { el.style.display = 'none'; el.removeEventListener('transitionend', onEnd); };
    el.addEventListener('transitionend', onEnd);
    // Fallback if transition doesn't fire
    setTimeout(() => { if (!isOpen) el.style.display = 'none'; }, 250);
    isOpen = false;
  }

  function toggle(): void { isOpen ? close() : open(); }

  document.body.append(el);
  return { open, close, toggle, isOpen: () => isOpen };
}
