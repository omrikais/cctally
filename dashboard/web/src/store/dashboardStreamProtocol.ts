import type { Envelope } from '../types/envelope';

export const DASHBOARD_STREAM_PROTOCOL_VERSION = 1 as const;

export type DashboardStreamClientMessage =
  | { version: 1; type: 'subscribe'; generation: number }
  | { version: 1; type: 'suspend'; generation: number }
  | { version: 1; type: 'resume'; generation: number }
  | { version: 1; type: 'unsubscribe'; generation: number };

export type DashboardStreamWorkerMessage =
  | { version: 1; type: 'ready'; generation: number }
  | {
      version: 1;
      type: 'snapshot';
      generation: number;
      deliveryGeneration: number;
      snapshot: Envelope;
    }
  | { version: 1; type: 'stream_error'; generation: number };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value != null && !Array.isArray(value);
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

export function isEnvelope(value: unknown): value is Envelope {
  if (!isRecord(value) || value.envelope_version !== 2
      || typeof value.generated_at !== 'string'
      || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value.generated_at)
      || !Number.isFinite(Date.parse(value.generated_at))) return false;
  const nullableRecord = (field: unknown) => field == null || isRecord(field);
  return isRecord(value.header)
    && nullableRecord(value.current_week)
    && nullableRecord(value.forecast)
    && nullableRecord(value.trend)
    && isRecord(value.weekly) && Array.isArray(value.weekly.rows)
    && isRecord(value.monthly) && Array.isArray(value.monthly.rows)
    && isRecord(value.blocks) && Array.isArray(value.blocks.rows)
    && isRecord(value.daily) && Array.isArray(value.daily.rows)
    && isRecord(value.sessions) && Array.isArray(value.sessions.rows)
    && nullableRecord(value.projects)
    && isRecord(value.display)
    && Array.isArray(value.alerts)
    && isRecord(value.alerts_settings);
}

export function isDashboardStreamClientMessage(
  value: unknown,
): value is DashboardStreamClientMessage {
  if (!isRecord(value) || value.version !== DASHBOARD_STREAM_PROTOCOL_VERSION
      || !isPositiveInteger(value.generation)) return false;
  return value.type === 'subscribe' || value.type === 'suspend'
    || value.type === 'resume' || value.type === 'unsubscribe';
}

export function isDashboardStreamWorkerMessage(
  value: unknown,
): value is DashboardStreamWorkerMessage {
  if (!isRecord(value) || value.version !== DASHBOARD_STREAM_PROTOCOL_VERSION
      || !isPositiveInteger(value.generation)) return false;
  if (value.type === 'ready' || value.type === 'stream_error') return true;
  return value.type === 'snapshot'
    && isPositiveInteger(value.deliveryGeneration)
    && isEnvelope(value.snapshot);
}
