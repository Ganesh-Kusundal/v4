import { describe, it, expect } from "vitest";
import {
  validateQuantity,
  validatePrice,
  validateOrder,
  withinPriceBand,
  type OrderConstraints,
} from "./validation";

const baseConstraints: OrderConstraints = {
  tickSize: 0.05,
  priceBand: { lower: 90, upper: 110 },
  freezeQty: 1000,
  lotSize: 10,
};

describe("validateQuantity", () => {
  it("accepts a valid positive quantity on the lot grid", () => {
    expect(validateQuantity(100, baseConstraints)).toEqual({ ok: true });
  });

  it("accepts quantity of 1 when no lot size is set", () => {
    const c: OrderConstraints = { tickSize: 0.05 };
    expect(validateQuantity(1, c)).toEqual({ ok: true });
  });

  it("rejects zero quantity", () => {
    const r = validateQuantity(0, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("QTY_INVALID");
  });

  it("rejects negative quantity", () => {
    const r = validateQuantity(-5, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("QTY_INVALID");
  });

  it("rejects non-finite quantity", () => {
    const r = validateQuantity(NaN, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("QTY_INVALID");
  });

  it("rejects quantity exceeding freeze limit", () => {
    // Use a qty on the lot grid (multiple of 10) but exceeding freeze (1000)
    const r = validateQuantity(1010, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("QTY_FREEZE_LIMIT");
  });

  it("rejects off-lot-grid quantity", () => {
    const r = validateQuantity(15, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("QTY_STEP");
  });

  it("rejects fractional quantity when not allowed", () => {
    const c: OrderConstraints = { tickSize: 0.05 };
    const r = validateQuantity(1.5, c);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("QTY_STEP");
  });

  it("accepts fractional quantity when allowFractionalQty is true", () => {
    const c: OrderConstraints = { tickSize: 0.05, allowFractionalQty: true };
    expect(validateQuantity(1.5, c)).toEqual({ ok: true });
  });

  it("accepts fractional quantity on a fractional lot grid", () => {
    const c: OrderConstraints = { tickSize: 0.05, lotSize: 0.1 };
    expect(validateQuantity(0.3, c)).toEqual({ ok: true });
  });
});

describe("validatePrice", () => {
  it("snaps price to tick size", () => {
    const r = validatePrice(100.07, baseConstraints);
    expect(r.ok).toBe(true);
    expect(r.price).toBeCloseTo(100.05, 10);
  });

  it("accepts price within band", () => {
    const r = validatePrice(100, baseConstraints);
    expect(r.ok).toBe(true);
    expect(r.price).toBe(100);
  });

  it("rejects price below band", () => {
    const r = validatePrice(80, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("PRICE_OUT_OF_BAND");
  });

  it("rejects price above band", () => {
    const r = validatePrice(120, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("PRICE_OUT_OF_BAND");
  });

  it("rejects non-finite price (NaN)", () => {
    const r = validatePrice(NaN, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("PRICE_INVALID");
  });

  it("rejects non-finite price (Infinity)", () => {
    const r = validatePrice(Infinity, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("PRICE_INVALID");
  });

  it("accepts price at band boundaries", () => {
    expect(validatePrice(90, baseConstraints).ok).toBe(true);
    expect(validatePrice(110, baseConstraints).ok).toBe(true);
  });

  it("returns snapped price when no band is set", () => {
    const c: OrderConstraints = { tickSize: 0.05 };
    const r = validatePrice(100.07, c);
    expect(r.ok).toBe(true);
    expect(r.price).toBeCloseTo(100.05, 10);
  });
});

describe("withinPriceBand", () => {
  it("returns true for price inside band", () => {
    expect(withinPriceBand(100, { lower: 90, upper: 110 })).toBe(true);
  });

  it("returns true at band boundaries", () => {
    expect(withinPriceBand(90, { lower: 90, upper: 110 })).toBe(true);
    expect(withinPriceBand(110, { lower: 90, upper: 110 })).toBe(true);
  });

  it("returns false outside band", () => {
    expect(withinPriceBand(89, { lower: 90, upper: 110 })).toBe(false);
    expect(withinPriceBand(111, { lower: 90, upper: 110 })).toBe(false);
  });
});

describe("validateOrder", () => {
  it("validates a market order (no price) with valid qty", () => {
    const r = validateOrder(undefined, 100, baseConstraints);
    expect(r.ok).toBe(true);
    expect(r.price).toBeUndefined();
  });

  it("validates a limit order with valid price and qty", () => {
    const r = validateOrder(100, 100, baseConstraints);
    expect(r.ok).toBe(true);
    expect(r.price).toBe(100);
  });

  it("rejects market order with invalid qty", () => {
    const r = validateOrder(undefined, 0, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("QTY_INVALID");
  });

  it("rejects limit order with bad qty even if price is valid", () => {
    const r = validateOrder(100, 0, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("QTY_INVALID");
  });

  it("rejects limit order with valid qty but out-of-band price", () => {
    const r = validateOrder(200, 100, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("PRICE_OUT_OF_BAND");
  });

  it("reports qty error first when both are invalid", () => {
    const r = validateOrder(200, 0, baseConstraints);
    expect(r.ok).toBe(false);
    expect(r.code).toBe("QTY_INVALID");
  });
});
