import { AsyncLocalStorage } from 'node:async_hooks';

// A content-based permit also permits the native copy of the very same answer.
// This authority follows only the awaited, persisted-outbox call, including when
// the official adapter runs in another worker with no global plugin hook runner.
const deliveries = new AsyncLocalStorage();
export const isHouKongManagedProfile = () => Boolean(process.env.HOUKONG_WECHAT_SERVICE_TOKEN);

export async function withHouKongOutboxDelivery({ accountId, to, outboxId, authorize }, send) {
  if (!accountId || !to || !outboxId || typeof authorize !== 'function' || typeof send !== 'function') {
    throw new Error('A persisted delivery authority is required.');
  }
  const scope = { accountId, to, outboxId, authorize, active: true };
  try { return await deliveries.run(scope, send); }
  finally { scope.active = false; }
}

export async function authorizeHouKongSend(params) {
  if (!isHouKongManagedProfile()) return true;
  const scope = deliveries.getStore();
  if (!scope?.active) return false;
  if (params?.body?.msg?.to_user_id !== scope.to || params.body.msg.run_id) {
    throw new Error('The delivery recipient or originating run does not match.');
  }
  // Recheck binding epoch for every actual API request, including file captions.
  await scope.authorize();
  return true;
}
