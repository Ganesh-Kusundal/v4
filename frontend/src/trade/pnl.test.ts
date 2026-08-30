import { describe, it, expect } from "vitest";
import {
  unrealizedPnl,
  unrealizedPnlPercent,
  riskReward,
  bracketValid,
} from "./pnl";
import type { Position } from "./types";

describe("unrealizedPnl", () => {
  it("computes long profit when ltp > avgPrice", () => {
    const pos: Position = { symbol: "A", netQty: 10, avgPrice: 100 };
    expect(unrealizedPnl(pos, 110)).toBe(100); // (110-100)*10
  });

  it("computes long loss when ltp < avgPrice", () => {
    const pos: Position = { symbol: "A", netQty: 10, avgPrice: 100 };
    expect(unrealizedPnl(pos, 90)).toBe(-100); // (90-100)*10
  });

  it("computes short profit when ltp < avgPrice", () => {
    const pos: Position = { symbol: "A", netQty: -10, avgPrice: 100 };
    expect(unrealizedPnl(pos, 90)).toBe(100); // (90-100)*(-10)
  });

  it("computes short loss when ltp > avgPrice", () => {
    const pos: Position = { symbol: "A", netQty: -10, avgPrice: 100 };
    expect(unrealizedPnl(pos, 110)).toBe(-100); // (110-100)*(-10)
  });

  it("returns 0 for flat position", () => {
    const pos: Position = { symbol: "A", netQty: 0, avgPrice: 100 };
    expect(unrealizedPnl(pos, 110)).toBe(0);
  });

  it("returns 0 when ltp equals avgPrice", () => {
    const pos: Position = { symbol: "A", netQty: 10, avgPrice: 100 };
    expect(unrealizedPnl(pos, 100)).toBe(0);
  });
});

describe("unrealizedPnlPercent", () => {
  it("returns positive percent for long profit", () => {
    const pos: Position = { symbol: "A", netQty: 10, avgPrice: 100 };
    expect(unrealizedPnlPercent(pos, 110)).toBe(10); // ((110-100)/100)*100*1
  });

  it("returns negative percent for long loss", () => {
    const pos: Position = { symbol: "A", netQty: 10, avgPrice: 100 };
    expect(unrealizedPnlPercent(pos, 90)).toBe(-10); // ((90-100)/100)*100*1
  });

  it("returns positive percent for short profit", () => {
    const pos: Position = { symbol: "A", netQty: -10, avgPrice: 100 };
    expect(unrealizedPnlPercent(pos, 90)).toBe(10); // ((90-100)/100)*100*(-1)
  });

  it("returns negative percent for short loss", () => {
    const pos: Position = { symbol: "A", netQty: -10, avgPrice: 100 };
    expect(unrealizedPnlPercent(pos, 110)).toBe(-10); // ((110-100)/100)*100*(-1)
  });

  it("returns 0 when avgPrice is 0", () => {
    const pos: Position = { symbol: "A", netQty: 10, avgPrice: 0 };
    expect(unrealizedPnlPercent(pos, 110)).toBe(0);
  });

  it("returns 0 when netQty is 0", () => {
    const pos: Position = { symbol: "A", netQty: 0, avgPrice: 100 };
    expect(unrealizedPnlPercent(pos, 110)).toBe(0);
  });
});

describe("riskReward", () => {
  it("computes R:R for a valid bracket", () => {
    // entry=100, stop=90, target=120 → risk=10, reward=20 → 2.0
    expect(riskReward(100, 90, 120)).toBe(2.0);
  });

  it("computes R:R for a short bracket", () => {
    // entry=100, stop=110, target=80 → risk=10, reward=20 → 2.0
    expect(riskReward(100, 110, 80)).toBe(2.0);
  });

  it("returns 1.0 when reward equals risk", () => {
    expect(riskReward(100, 90, 110)).toBe(1.0);
  });

  it("returns null when risk is zero (stop equals entry)", () => {
    expect(riskReward(100, 100, 120)).toBeNull();
  });

  it("returns fractional R:R when reward < risk", () => {
    // entry=100, stop=95, target=110 → risk=5, reward=10 → 2.0
    expect(riskReward(100, 95, 110)).toBe(2.0);
  });
});

describe("bracketValid", () => {
  it("validates BUY: stop < entry < target", () => {
    expect(bracketValid("BUY", 100, 90, 110)).toBe(true);
  });

  it("rejects BUY when stop >= entry", () => {
    expect(bracketValid("BUY", 100, 100, 110)).toBe(false);
    expect(bracketValid("BUY", 100, 110, 120)).toBe(false);
  });

  it("rejects BUY when target <= entry", () => {
    expect(bracketValid("BUY", 100, 90, 100)).toBe(false);
    expect(bracketValid("BUY", 100, 90, 80)).toBe(false);
  });

  it("validates SELL: stop > entry > target", () => {
    expect(bracketValid("SELL", 100, 110, 90)).toBe(true);
  });

  it("rejects SELL when stop <= entry", () => {
    expect(bracketValid("SELL", 100, 100, 90)).toBe(false);
    expect(bracketValid("SELL", 100, 90, 80)).toBe(false);
  });

  it("rejects SELL when target >= entry", () => {
    expect(bracketValid("SELL", 100, 110, 100)).toBe(false);
    expect(bracketValid("SELL", 100, 110, 120)).toBe(false);
  });
});
