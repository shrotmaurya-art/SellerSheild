# CONTRACT COVERAGE: Store & Agent OpenAPI Contracts (Milestone M0)

This document maps every endpoint row from the system specifications (`01-playground.md` Section 6 and Section 16 amendments for Store API, and `02-agent.md` Section 11 and Section 21 amendments for Agent API) to its OpenAPI operationId in `contracts/`.

## 1. Store API Coverage (`contracts/openapi-store.yaml`)

| Group | Section 6 Row / Amendment | Specification Endpoint | OperationId | Status |
|---|---|---|---|---|
| Auth | POST /auth/register (P1) | `/auth/register` | `register_customer` | COVERED |
| Auth | POST /auth/login | `/auth/login` | `login` | COVERED |
| Auth | POST /auth/refresh | `/auth/refresh` | `refresh_token` | COVERED |
| Auth | POST /auth/logout | `/auth/logout` | `logout` | COVERED |
| Auth | GET /auth/me | `/auth/me` | `get_current_user` | COVERED |
| Auth | GET /.well-known/jwks.json | `/.well-known/jwks.json` | `get_jwks` | COVERED |
| Auth | POST /auth/token (service credentials) | `/auth/token` | `issue_service_token` | COVERED |
| Auth | GET /clock (Amendment 10, always-on) | `/clock` | `get_public_clock` | COVERED |
| Orders | GET /orders (own orders only) | `/orders` | `list_orders` | COVERED |
| Orders | GET /orders/{id} (uniform 404) | `/orders/{id}` | `get_order` | COVERED |
| Orders | GET /orders/{id}/shipment | `/orders/{id}/shipment` | `get_order_shipment` | COVERED |
| Orders | POST /orders/{id}/evidence (Amendment 4) | `/orders/{id}/evidence` | `upload_order_evidence` | COVERED |
| Orders | GET /orders/{id}/evidence (Amendment 4) | `/orders/{id}/evidence` | `list_order_evidence` | COVERED |
| Orders | GET /evidence/{id} (Amendment 4) | `/evidence/{id}` | `get_evidence` | COVERED |
| Orders | GET /orders/{id}/refundable (Amendment 5) | `/orders/{id}/refundable` | `get_order_refundable` | COVERED |
| Returns | POST /orders/{id}/returns | `/orders/{id}/returns` | `create_return` | COVERED |
| Returns | GET /returns | `/returns` | `list_returns` | COVERED |
| Returns | GET /returns/{id} | `/returns/{id}` | `get_return` | COVERED |
| Returns | POST /returns/{id}/transition | `/returns/{id}/transition` | `transition_return` | COVERED |
| Returns | POST /returns/{id}/cancel | `/returns/{id}/cancel` | `cancel_return` | COVERED |
| Refunds | POST /orders/{id}/refund-reservations | `/orders/{id}/refund-reservations` | `create_refund_reservation` | COVERED |
| Refunds | POST /refunds/{id}/authorize (Amendment 2) | `/refunds/{id}/authorize` | `authorize_refund` | COVERED |
| Refunds | POST /refunds/{id}/release | `/refunds/{id}/release` | `release_refund` | COVERED |
| Refunds | POST /refunds/{id}/cancel-reservation | `/refunds/{id}/cancel-reservation` | `cancel_refund_reservation` | COVERED |
| Refunds | GET /refunds/{id} | `/refunds/{id}` | `get_refund` | COVERED |
| Refunds | GET /orders/{id}/refunds | `/orders/{id}/refunds` | `list_order_refunds` | COVERED |
| Refunds | GET /customers/{id}/refund-summary?days=30 | `/customers/{id}/refund-summary` | `get_customer_refund_summary` | COVERED |
| Admin | GET /admin/orders | `/admin/orders` | `admin_list_orders` | COVERED |
| Admin | GET /admin/orders/{id} | `/admin/orders/{id}` | `admin_get_order` | COVERED |
| Admin | GET /admin/customers/{id} | `/admin/customers/{id}` | `admin_get_customer` | COVERED |
| Admin | POST /admin/returns/{id}/receive | `/admin/returns/{id}/receive` | `admin_receive_return` | COVERED |
| Admin | POST /admin/returns/{id}/inspect | `/admin/returns/{id}/inspect` | `admin_inspect_return` | COVERED |
| Admin | POST /admin/orders/{id}/cancel | `/admin/orders/{id}/cancel` | `admin_cancel_order` | COVERED |
| Admin | GET /admin/settings (Amendment 1) | `/admin/settings` | `admin_get_settings` | COVERED |
| Admin | PUT /admin/settings (Amendment 1) | `/admin/settings` | `admin_update_settings` | COVERED |
| Mail | GET /mail/inbox | `/mail/inbox` | `list_mail_messages` | COVERED |
| Mail | GET /mail/threads/{id} | `/mail/threads/{id}` | `get_mail_thread` | COVERED |
| Mail | POST /mail/send | `/mail/send` | `send_mail_message` | COVERED |
| Mail | POST /mail/{id}/read | `/mail/{id}/read` | `mark_mail_read` | COVERED |
| Sim | GET /sim/carrier/shipments/{id} | `/sim/carrier/shipments/{id}` | `sim_get_carrier_shipment` | COVERED |
| Sim | POST /sim/carrier/shipments/{id}/advance | `/sim/carrier/shipments/{id}/advance` | `sim_advance_carrier_shipment` | COVERED |
| Sim | POST /sim/carrier/scenarios/{id}/play | `/sim/carrier/scenarios/{id}/play` | `sim_play_carrier_scenario` | COVERED |
| Demo | POST /demo/reset | `/demo/reset` | `demo_reset` | COVERED |
| Demo | GET /demo/clock | `/demo/clock` | `demo_get_clock` | COVERED |
| Demo | POST /demo/clock | `/demo/clock` | `demo_set_clock` | COVERED |
| Demo | GET /demo/faults | `/demo/faults` | `demo_get_faults` | COVERED |
| Demo | POST /demo/faults | `/demo/faults` | `demo_set_faults` | COVERED |
| Demo | GET /demo/state | `/demo/state` | `demo_get_state` | COVERED |
| Demo | POST /demo/scenarios/{id}/load | `/demo/scenarios/{id}/load` | `demo_load_scenario` | COVERED |

### Store Webhook Outbox Events Coverage

| Event Type | OpenAPI 3.1 Webhook Name | Payload Model | Headers | Status |
|---|---|---|---|---|
| `order.created` | `order.created` | `WebhookEnvelope` | `X-Store-Signature`, `X-Store-Timestamp`, `X-Event-Id` | COVERED |
| `order.updated` | `order.updated` | `WebhookEnvelope` | `X-Store-Signature`, `X-Store-Timestamp`, `X-Event-Id` | COVERED |
| `shipment.updated` | `shipment.updated` | `WebhookEnvelope` | `X-Store-Signature`, `X-Store-Timestamp`, `X-Event-Id` | COVERED |
| `shipment.delayed` | `shipment.delayed` | `WebhookEnvelope` | `X-Store-Signature`, `X-Store-Timestamp`, `X-Event-Id` | COVERED |
| `return.updated` | `return.updated` | `WebhookEnvelope` | `X-Store-Signature`, `X-Store-Timestamp`, `X-Event-Id` | COVERED |
| `refund.updated` | `refund.updated` | `WebhookEnvelope` | `X-Store-Signature`, `X-Store-Timestamp`, `X-Event-Id` | COVERED |
| `mail.inbound` | `mail.inbound` | `WebhookEnvelope` | `X-Store-Signature`, `X-Store-Timestamp`, `X-Event-Id` | COVERED |

---

## 2. Agent API Coverage (`contracts/openapi-agent.yaml`)

| Group | Section 11 Row / Amendment | Specification Endpoint | OperationId | Status |
|---|---|---|---|---|
| Chat | POST /v1/chat/sessions | `/chat/sessions` | `create_chat_session` | COVERED |
| Chat | POST /v1/chat/sessions/{id}/messages | `/chat/sessions/{id}/messages` | `send_chat_message` | COVERED |
| Chat | GET /v1/chat/sessions/{id} | `/chat/sessions/{id}` | `get_chat_session` | COVERED |
| Chat | GET /v1/chat/stream (Amendment 3) | `/chat/stream` | `stream_chat_push_events` | COVERED |
| Chat | GET /v1/me/events?since= (Amendment 3) | `/me/events` | `poll_customer_events` | COVERED |
| Proposals | POST /v1/proposals/{id}/confirm | `/proposals/{id}/confirm` | `confirm_proposal` | COVERED |
| Proposals | POST /v1/proposals/{id}/cancel | `/proposals/{id}/cancel` | `cancel_proposal` | COVERED |
| Customer Support | POST /v1/support/request-human | `/support/request-human` | `request_human_support` | COVERED |
| Customer Support | GET /v1/me/cases | `/me/cases` | `list_customer_cases` | COVERED |
| Customer Support | GET /v1/me/cases/{id} | `/me/cases/{id}` | `get_customer_case` | COVERED |
| Customer Support | POST /v1/me/cases/{id}/messages | `/me/cases/{id}/messages` | `send_customer_case_message` | COVERED |
| Customer Support | DELETE /v1/me/memory | `/me/memory` | `erase_customer_memory` | COVERED |
| Seller Approvals | GET /v1/seller/approvals | `/seller/approvals` | `list_seller_approvals` | COVERED |
| Seller Approvals | GET /v1/seller/approvals/{id} | `/seller/approvals/{id}` | `get_seller_approval` | COVERED |
| Seller Approvals | POST /v1/seller/approvals/{id}/decision (Amendment 9) | `/seller/approvals/{id}/decision` | `decide_seller_approval` | COVERED |
| Seller Cases | GET /v1/seller/cases | `/seller/cases` | `list_seller_cases` | COVERED |
| Seller Cases | GET /v1/seller/cases/{id} | `/seller/cases/{id}` | `get_seller_case` | COVERED |
| Seller Cases | GET /v1/seller/cases/{id}/brief (Amendment 6c) | `/seller/cases/{id}/brief` | `get_seller_case_brief` | COVERED |
| Seller Cases | POST /v1/seller/cases/{id}/messages | `/seller/cases/{id}/messages` | `send_seller_case_message` | COVERED |
| Seller Cases | POST /v1/seller/cases/{id}/status | `/seller/cases/{id}/status` | `update_seller_case_status` | COVERED |
| Seller Cases | POST /v1/seller/cases/{id}/assign | `/seller/cases/{id}/assign` | `assign_seller_case` | COVERED |
| Seller Cases | POST /v1/seller/cases/{id}/handback | `/seller/cases/{id}/handback` | `handback_seller_case` | COVERED |
| Seller Ops | GET /v1/seller/overview | `/seller/overview` | `get_seller_overview` | COVERED |
| Seller Ops | GET /v1/seller/activity (SSE) | `/seller/activity` | `stream_seller_activity` | COVERED |
| Seller Ops | GET /v1/seller/traces/{request_id} | `/seller/traces/{request_id}` | `get_seller_trace` | COVERED |
| Seller Ops | GET /v1/seller/audit | `/seller/audit` | `list_seller_audit_events` | COVERED |
| Seller Ops | GET /v1/seller/audit/verify | `/seller/audit/verify` | `verify_seller_audit_chain` | COVERED |
| Seller Ops | GET /v1/seller/notifications | `/seller/notifications` | `list_seller_notifications` | COVERED |
| Seller Ops | POST /v1/seller/notifications/{id}/retry | `/seller/notifications/{id}/retry` | `retry_seller_notification` | COVERED |
| Seller Ops | GET /v1/seller/analytics | `/seller/analytics` | `get_seller_analytics` | COVERED |
| Seller Ops | POST /v1/seller/monitor/run | `/seller/monitor/run` | `run_seller_monitor` | COVERED |
| Policy Studio | GET /v1/seller/policies | `/seller/policies` | `list_seller_policies` | COVERED |
| Policy Studio | POST /v1/seller/policies | `/seller/policies` | `create_seller_policy` | COVERED |
| Policy Studio | GET /v1/seller/policies/active | `/seller/policies/active` | `get_active_seller_policy` | COVERED |
| Policy Studio | GET /v1/seller/policies/{version} | `/seller/policies/{version}` | `get_seller_policy` | COVERED |
| Policy Studio | PUT /v1/seller/policies/{version} | `/seller/policies/{version}` | `update_seller_policy` | COVERED |
| Policy Studio | POST /v1/seller/policies/{version}/activate (Amendment 9) | `/seller/policies/{version}/activate` | `activate_seller_policy` | COVERED |
| Policy Studio | POST /v1/seller/policies/simulate | `/seller/policies/simulate` | `simulate_seller_policy` | COVERED |
| System & Dev | POST /v1/webhooks/store | `/webhooks/store` | `receive_store_webhook` | COVERED |
| System & Dev | GET /healthz | `/healthz` | `get_agent_healthz` | COVERED |
| System & Dev | GET /readyz | `/readyz` | `get_agent_readyz` | COVERED |
| System & Dev | GET /v1/seller/monitor/runs | `/seller/monitor/runs` | `list_seller_monitor_runs` | COVERED |
| System & Dev | GET /v1/seller/monitor/runs/{id} | `/seller/monitor/runs/{id}` | `get_seller_monitor_run` | COVERED |
| System & Dev | POST /v1/dev/faults | `/dev/faults` | `dev_set_faults` | COVERED |
| System & Dev | GET /v1/dev/faults | `/dev/faults` | `dev_get_faults` | COVERED |
| System & Dev | POST /v1/dev/eval/run | `/dev/eval/run` | `dev_run_eval` | COVERED |
| System & Dev | GET /v1/dev/eval/runs | `/dev/eval/runs` | `dev_list_eval_runs` | COVERED |
| System & Dev | GET /v1/dev/eval/runs/{id} | `/dev/eval/runs/{id}` | `dev_get_eval_run` | COVERED |
