import { describe, it, expect } from "vitest";
import {
  transition,
  canTransition,
  isTerminal,
  type ClientOrderState,
  type OrderEvent,
} from "./order-state-machine";

describe("order-state-machine transitions", () => {
  describe("pending_place", () => {
    it("transitions to working on ack", () => {
      expect(transition("pending_place", "ack")).toBe("working");
    });

    it("transitions to partial on partialFill", () => {
      expect(transition("pending_place", "partialFill")).toBe("partial");
    });

    it("transitions to filled on fill", () => {
      expect(transition("pending_place", "fill")).toBe("filled");
    });

    it("transitions to rejected on reject", () => {
      expect(transition("pending_place", "reject")).toBe("rejected");
    });

    it("transitions to stale on reconnectAbsent", () => {
      expect(transition("pending_place", "reconnectAbsent")).toBe("stale");
    });
  });

  describe("working", () => {
    it("transitions to partial on partialFill", () => {
      expect(transition("working", "partialFill")).toBe("partial");
    });

    it("transitions to filled on fill", () => {
      expect(transition("working", "fill")).toBe("filled");
    });

    it("transitions to rejected on reject", () => {
      expect(transition("working", "reject")).toBe("rejected");
    });

    it("transitions to modify_pending on submitModify", () => {
      expect(transition("working", "submitModify")).toBe("modify_pending");
    });

    it("transitions to cancel_pending on submitCancel", () => {
      expect(transition("working", "submitCancel")).toBe("cancel_pending");
    });

    it("transitions to cancelled on cancelled", () => {
      expect(transition("working", "cancelled")).toBe("cancelled");
    });

    it("transitions to stale on reconnectAbsent", () => {
      expect(transition("working", "reconnectAbsent")).toBe("stale");
    });
  });

  describe("partial", () => {
    it("stays in partial on partialFill", () => {
      expect(transition("partial", "partialFill")).toBe("partial");
    });

    it("transitions to filled on fill", () => {
      expect(transition("partial", "fill")).toBe("filled");
    });

    it("transitions to rejected on reject", () => {
      expect(transition("partial", "reject")).toBe("rejected");
    });

    it("transitions to modify_pending on submitModify", () => {
      expect(transition("partial", "submitModify")).toBe("modify_pending");
    });

    it("transitions to cancel_pending on submitCancel", () => {
      expect(transition("partial", "submitCancel")).toBe("cancel_pending");
    });

    it("transitions to cancelled on cancelled", () => {
      expect(transition("partial", "cancelled")).toBe("cancelled");
    });

    it("transitions to stale on reconnectAbsent", () => {
      expect(transition("partial", "reconnectAbsent")).toBe("stale");
    });
  });

  describe("modify_pending", () => {
    it("transitions to working on ack", () => {
      expect(transition("modify_pending", "ack")).toBe("working");
    });

    it("transitions to working on reject (modify failed)", () => {
      expect(transition("modify_pending", "reject")).toBe("working");
    });

    it("transitions to filled on fill", () => {
      expect(transition("modify_pending", "fill")).toBe("filled");
    });

    it("transitions to partial on partialFill", () => {
      expect(transition("modify_pending", "partialFill")).toBe("partial");
    });

    it("transitions to cancelled on cancelled", () => {
      expect(transition("modify_pending", "cancelled")).toBe("cancelled");
    });

    it("transitions to stale on reconnectAbsent", () => {
      expect(transition("modify_pending", "reconnectAbsent")).toBe("stale");
    });
  });

  describe("cancel_pending", () => {
    it("transitions to cancelled on cancelled", () => {
      expect(transition("cancel_pending", "cancelled")).toBe("cancelled");
    });

    it("transitions to working on reject (cancel failed)", () => {
      expect(transition("cancel_pending", "reject")).toBe("working");
    });

    it("transitions to filled on fill", () => {
      expect(transition("cancel_pending", "fill")).toBe("filled");
    });

    it("transitions to stale on reconnectAbsent", () => {
      expect(transition("cancel_pending", "reconnectAbsent")).toBe("stale");
    });
  });

  describe("terminal states accept nothing", () => {
    const terminals: ClientOrderState[] = ["filled", "cancelled", "rejected", "stale"];
    const events: OrderEvent[] = [
      "ack",
      "partialFill",
      "fill",
      "reject",
      "submitModify",
      "submitCancel",
      "cancelled",
      "reconnectAbsent",
    ];

    for (const state of terminals) {
      it(`${state} stays on any event`, () => {
        for (const event of events) {
          expect(transition(state, event)).toBe(state);
        }
      });
    }
  });

  describe("invalid events return same state", () => {
    it("working does not accept ack", () => {
      expect(transition("working", "ack")).toBe("working");
    });

    it("pending_place does not accept submitModify", () => {
      expect(transition("pending_place", "submitModify")).toBe("pending_place");
    });

    it("filled does not accept any event", () => {
      expect(transition("filled", "ack")).toBe("filled");
      expect(transition("filled", "fill")).toBe("filled");
    });
  });
});

describe("canTransition", () => {
  it("returns true for valid transitions", () => {
    expect(canTransition("pending_place", "ack")).toBe(true);
    expect(canTransition("working", "fill")).toBe(true);
  });

  it("returns false for invalid transitions", () => {
    expect(canTransition("working", "ack")).toBe(false);
    expect(canTransition("filled", "ack")).toBe(false);
  });
});

describe("isTerminal", () => {
  it("returns true for terminal states", () => {
    expect(isTerminal("filled")).toBe(true);
    expect(isTerminal("cancelled")).toBe(true);
    expect(isTerminal("rejected")).toBe(true);
    expect(isTerminal("stale")).toBe(true);
  });

  it("returns false for non-terminal states", () => {
    expect(isTerminal("pending_place")).toBe(false);
    expect(isTerminal("working")).toBe(false);
    expect(isTerminal("partial")).toBe(false);
    expect(isTerminal("modify_pending")).toBe(false);
    expect(isTerminal("cancel_pending")).toBe(false);
  });
});
