/**
 * The Lightweight Charts series primitive that paints the drawings (SPEC section 10:
 * "custom primitives"). It holds no state; it asks the controller on every repaint.
 */

import type {
  ISeriesPrimitive,
  ISeriesPrimitiveAxisView,
  PrimitiveHoveredItem,
  SeriesAttachedParameter,
  Time,
} from "lightweight-charts";

import type { DrawingController } from "./controller";

/** The part of the library's canvas target the drawing code uses. */
export interface MediaTarget {
  useMediaCoordinateSpace<T>(
    f: (scope: { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } }) => T,
  ): T;
}

class AxisLabel implements ISeriesPrimitiveAxisView {
  constructor(
    private readonly pos: number | null,
    private readonly label: string,
    private readonly color: string,
  ) {}

  coordinate(): number {
    return this.pos ?? -1000;
  }

  text(): string {
    return this.label;
  }

  textColor(): string {
    return "#ffffff";
  }

  backColor(): string {
    return this.color;
  }

  visible(): boolean {
    return this.pos !== null;
  }

  tickVisible(): boolean {
    return true;
  }
}

export class DrawingLayer implements ISeriesPrimitive<Time> {
  private params?: SeriesAttachedParameter<Time>;
  private readonly view = {
    zOrder: () => "top" as const,
    renderer: () => ({ draw: (target: unknown) => this.ctl.draw(target as MediaTarget) }),
  };

  constructor(private readonly ctl: DrawingController) {}

  attached(params: SeriesAttachedParameter<Time>): void {
    this.params = params;
  }

  detached(): void {
    this.params = undefined;
  }

  requestUpdate(): void {
    this.params?.requestUpdate();
  }

  paneViews() {
    return [this.view];
  }

  priceAxisViews(): ISeriesPrimitiveAxisView[] {
    const a = this.ctl.axisPoints();
    if (!a?.price) return [];
    const prices = [...new Set(a.points.map((p) => p.p))];
    return prices.map((p) => new AxisLabel(this.ctl.priceY(p), this.ctl.formatPrice(p), a.color));
  }

  timeAxisViews(): ISeriesPrimitiveAxisView[] {
    const a = this.ctl.axisPoints();
    if (!a?.time) return [];
    return a.points.map((p) => new AxisLabel(this.ctl.timeX(p.t), this.ctl.formatTime(p.t), a.color));
  }

  hitTest(x: number, y: number): PrimitiveHoveredItem | null {
    const cursor = this.ctl.cursorFor(x, y);
    return cursor ? { externalId: "drawing", zOrder: "top", cursorStyle: cursor } : null;
  }
}
