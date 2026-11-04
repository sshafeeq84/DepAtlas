import authClient from "auth_client";
const sessionVerifier = require("auth_client/session");

function createCharge() {
  // Explicit endpoint registry match -> medium confidence
  fetch("https://auth.internal/session/validate");

  // No registry entry, but "shipping" appears in the URL and a
  // shipping-service is known via the manifest -> low confidence
  axios.post("https://internal-shipping-gateway.example.com/notify");
}
