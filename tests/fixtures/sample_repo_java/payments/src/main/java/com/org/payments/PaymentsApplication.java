package com.org.payments;

import com.org.auth.AuthClient;
import com.org.auth.session.SessionVerifier;
import java.util.List;
import org.springframework.web.client.RestTemplate;

@FeignClient(name = "notification-service")
interface NotificationFeignClient {
    void notify(String message);
}

public class PaymentsApplication {
    private RestTemplate restTemplate;

    public void createCharge() {
        // Explicit endpoint registry match -> medium confidence
        restTemplate.getForObject("https://auth.internal/session/validate", String.class);

        // No registry entry, but "shipping" appears in the URL and a
        // shipping-service is known via the manifest -> low confidence
        restTemplate.postForObject("https://internal-shipping-gateway.example.com/notify", null, String.class);
    }
}
