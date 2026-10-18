// Code node "Read token claims" (Workflow B). Mode: Run Once for All Items.
//
// The Webhook node has already verified the HS256 signature and the expiry (JWT Auth,
// DECISIONS.md AD11). This node adds the checks that node can't make: the batch id must be a
// UUID, and the token must be short-lived and not issued in the future, which bounds replay.
// Only the verified claims are used, never the request body.

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_LIFETIME_SECONDS = 600;
const CLOCK_SKEW_SECONDS = 60;

function claimsToBatch(claims, nowSeconds) {
  if (!claims || typeof claims !== 'object') throw new Error('no verified token claims');
  const { batch_id: batchId, iat, exp } = claims;
  if (!UUID.test(String(batchId ?? ''))) throw new Error('token batch_id is not a UUID');
  if (!Number.isInteger(iat) || !Number.isInteger(exp)) throw new Error('token lacks iat/exp');
  if (iat > nowSeconds + CLOCK_SKEW_SECONDS) throw new Error('token issued in the future');
  if (exp - iat > MAX_LIFETIME_SECONDS) throw new Error('token lifetime exceeds 10 minutes');
  return { batch_id: batchId.toLowerCase(), trigger: 'webhook', jti: claims.jti ?? null };
}

// ---- n8n entry point ----
return [{ json: claimsToBatch($input.first().json.jwtPayload, Math.floor(Date.now() / 1000)) }];
