/* Campaign creation is the only unauthenticated endpoint, so it is the only
 * one that can be hammered. One object per address, counting the hour.
 *
 * This used to be a row in D1, which meant a database write on every campaign
 * creation and a table that had to be swept. An object per address costs
 * nothing while idle and deletes itself an hour after it was last touched, so
 * nothing accumulates.
 */

import { MAX_CREATES_PER_HOUR, json } from "./shared.js";

const HOUR = 3600000;

export class RateLimiter {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
  }

  /* Overridable so local development is not fighting its own limit — see
     .dev.vars, which wrangler dev picks up and which is not deployed. */
  get limit() {
    const n = parseInt(this.env.MAX_CREATES_PER_HOUR, 10);
    return n > 0 ? n : MAX_CREATES_PER_HOUR;
  }

  async fetch() {
    const now = Date.now();
    const bucket = Math.floor(now / HOUR);
    const held = (await this.ctx.storage.get("bucket")) || { bucket, n: 0 };
    if (held.bucket !== bucket) {
      held.bucket = bucket;
      held.n = 0;
    }
    if (held.n >= this.limit) {
      return json({ ok: false }, 429);
    }
    held.n += 1;
    await this.ctx.storage.put("bucket", held);
    /* Nothing here is worth keeping once the hour is out. */
    await this.ctx.storage.setAlarm(now + HOUR + 60000);
    return json({ ok: true });
  }

  async alarm() {
    await this.ctx.storage.deleteAll();
  }
}
