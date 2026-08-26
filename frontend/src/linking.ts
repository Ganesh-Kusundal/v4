// Chart linking: a workspace binding for chart instances. Our shell owns one
// chart today, so the group carries the primary chart; a host that later owns
// more charts can add them (group.add(chart) per chart). Pure chrome — no
// backend involved.
import { createLinkGroup, LinkGroup, type LinkChart } from "openalgo-charts";

let group: LinkGroup | null = null;

export function ensureLinkGroup(): LinkGroup {
  if (group) return group;
  group = createLinkGroup({ crosshair: true, viewport: true, symbol: false, whenMissing: "nearest" });
  return group;
}

export function linkChart(chart: LinkChart): void {
  ensureLinkGroup().add(chart);
}

export function unlinkAll(): void {
  group?.destroy();
  group = null;
}