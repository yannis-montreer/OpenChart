/** How the preview instance of a Run differs from a real one on the chart. */

import type { SeriesPlan } from "./indicatorOutputs";

/** Lightweight Charts' `LineStyle.Dashed`. */
const DASHED = 2;

/**
 * Lines and areas dashed, and no value labels on the price scale: a preview must never be
 * mistaken for an indicator of the chart. Colours, widths and visibility are the file's own.
 */
export function dashPlans(plans: SeriesPlan[]): SeriesPlan[] {
  return plans.map((p) => ({
    ...p,
    options: { ...p.options, ...(p.kind === "histogram" ? {} : { lineStyle: DASHED }), lastValueVisible: false },
  }));
}

export function previewLabel(label: string): string {
  return `Preview: ${label}`;
}
