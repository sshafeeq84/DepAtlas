// Genuine TypeScript syntax (type annotations) -- esprima can't parse
// this. parse_repo should skip this file with a warning, not crash.
import authClient from "auth_client";

function verify(token: string): boolean {
  return token.length > 0;
}
