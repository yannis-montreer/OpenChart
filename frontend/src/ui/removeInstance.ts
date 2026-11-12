/** Removing an indicator from a chart: says which alerts will stop, and asks only when some will. */

import type { Alert } from "../api";
import { removeInstancePrompt } from "../alerts/confirmations";
import { confirmWithAlerts } from "./alertsConfirm";

/** `alerts`: the indicator alerts on that instance. Resolves true when the removal may go ahead. */
export async function confirmRemoveInstance(title: string, alerts: readonly Alert[]): Promise<boolean> {
  const prompt = removeInstancePrompt(title, alerts);
  if (!prompt.needed) return true;
  return confirmWithAlerts({ ...prompt, danger: true });
}
