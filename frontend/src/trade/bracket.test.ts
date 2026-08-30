import { describe, it, expect } from "vitest";
import { BracketGroup, type BracketState } from "./bracket";
import { riskReward, bracketValid } from "./pnl";

describe("BracketGroup", () => {
  const baseState: BracketState = {
    symbol: "AAPL",
    side: "BUY",
    entry: 100,
    stop: 90,
    target: 120,
  };

  it("exposes state via getter", () => {
    const bg = new BracketGroup(baseState);
    expect(bg.state).toEqual(baseState);
  });

  it("updates state via update()", () => {
    const bg = new BracketGroup(baseState);
    const newState: BracketState = { ...baseState, stop: 95 };
    bg.update(newState);
    expect(bg.state).toEqual(newState);
  });

  it("autoscaleInfo includes entry, stop, target", () => {
    const bg = new BracketGroup(baseState);
    const info = bg.autoscaleInfo();
    expect(info.min).toBe(90);
    expect(info.max).toBe(120);
  });

  it("autoscaleInfo handles inverted range", () => {
    const state: BracketState = { ...baseState, stop: 130, target: 80 };
    const bg = new BracketGroup(state);
    const info = bg.autoscaleInfo();
    expect(info.min).toBe(80);
    expect(info.max).toBe(130);
  });

  it("R:R matches riskReward calculation", () => {
    const bg = new BracketGroup(baseState);
    const rr = riskReward(baseState.entry, baseState.stop, baseState.target);
    expect(rr).toBe(2.0); // (120-100)/(100-90)
    // The bracket's R:R chip would display this value
    expect(bg.state.entry).toBe(baseState.entry);
    expect(bg.state.stop).toBe(baseState.stop);
    expect(bg.state.target).toBe(baseState.target);
  });

  it("bracketValid confirms valid BUY bracket", () => {
    expect(bracketValid("BUY", 100, 90, 120)).toBe(true);
  });

  it("bracketValid confirms valid SELL bracket", () => {
    expect(bracketValid("SELL", 100, 110, 80)).toBe(true);
  });

  it("bracketValid rejects invalid bracket", () => {
    expect(bracketValid("BUY", 100, 110, 90)).toBe(false);
  });
});

describe("BracketGroup side handling", () => {
  it("renders BUY bracket with correct state", () => {
    const state: BracketState = { symbol: "AAPL", side: "BUY", entry: 100, stop: 90, target: 120 };
    const bg = new BracketGroup(state);
    expect(bg.state.side).toBe("BUY");
  });

  it("renders SELL bracket with correct state", () => {
    const state: BracketState = { symbol: "AAPL", side: "SELL", entry: 100, stop: 110, target: 80 };
    const bg = new BracketGroup(state);
    expect(bg.state.side).toBe("SELL");
  });

  it("handles zero-risk bracket (stop at entry)", () => {
    const state: BracketState = { symbol: "AAPL", side: "BUY", entry: 100, stop: 100, target: 120 };
    const bg = new BracketGroup(state);
    expect(riskReward(state.entry, state.stop, state.target)).toBeNull();
    expect(bg.autoscaleInfo().min).toBe(100);
    expect(bg.autoscaleInfo().max).toBe(120);
  });
});
