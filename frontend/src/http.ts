// G13 typed-error contract: shared fetch helper that parses the backend's
// {"error": {"code": <string>, "message": <string>}} envelope on !resp.ok
// and throws an Error carrying both fields. Success-path parsing also goes
// through here so JSON parse errors surface consistently.

export interface ApiError {
  code: string;
  message: string;
}

/**
 * Parse a Response as JSON. On !resp.ok, reads the typed error envelope and
 * throws an Error whose `.message` is `"[{code}] {message}"`. Falls back to
 * `resp.statusText` when the body is not the expected envelope shape.
 */
export async function expectJson<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let msg = resp.statusText;
    try {
      const body = (await resp.json()) as { error?: { code?: string; message?: string } };
      if (body.error?.code && body.error?.message) {
        msg = `[${body.error.code}] ${body.error.message}`;
      }
    } catch {
      // non-JSON error body — statusText fallback already set
    }
    throw new Error(msg);
  }
  return resp.json() as Promise<T>;
}
