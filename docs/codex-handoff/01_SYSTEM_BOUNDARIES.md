# System Boundaries And Design Principles

## 1. Product Boundaries

GMV OPS is the application at `gmv.myupona.com`. Its production repository is
`/opt/gmv/GMV-OPS`.

Nextcloud at `pan.myupona.com` is a separate production system. It has its own
Talk, TURN, recording, SIP, mail, FFmpeg/QSV, storage, proxy, certificate, and
FRP concerns. Do not change Nextcloud while working on GMV OPS unless the user
explicitly asks for it.

The public relay host and local DNS topology are infrastructure dependencies,
not application authorization boundaries. Never infer a tenant or user from an
IP address, hostname resolution, or which browser happens to be reachable.

## 2. Tenancy And Ownership

The platform is multi-company and multi-user:

- `workspace_id` is the company boundary.
- `user_id` or `created_by_user_id` is the member boundary.
- A regular member sees only their own projects, tasks, files, and history.
- A workspace administrator may use dedicated admin/member views to inspect,
  preview, download, and package member outputs. Admin access must not merge
  member records into the administrator's personal project list.
- Company product-library rows belong to the workspace and may be selected by
  workspace members.
- Project execution, browser bridge, device, slot, stage, asset, AI task, and
  deliverable rows must retain both workspace and user ownership.

Every list, detail, content, download, ZIP, retry, delete, pause, and resume
endpoint must enforce the same ownership rules. Filesystem paths and task IDs
are never authorization.

## 3. Device And Browser Ownership

A browser bridge belongs to `(workspace_id, user_id, device_id)`.

- Users may bind multiple devices.
- If exactly one device is online, it may be selected automatically.
- If multiple devices are online, the user selects the active device.
- A slot on one device is not inherited by another device.
- A project is pinned to one slot for its browser-required lifetime.
- Different projects may run in parallel on different slots.
- Pausing, deleting, completing, or terminally failing a project releases its
  slot.
- API-only stages do not open or reserve Chrome.

Never route another user's or device's project into the first reachable CDP
port. Never use clipboard-based cross-device file transfer.

## 4. Storage And Asset Ownership

Primary Content Factory storage:

```text
/data/gmv_ops/hermes_content_factory/
  workspace_<workspace_id>/
    <project_key>/
  browser_inbox/
    workspace_<workspace_id>/
      <project_key>/
  browser_outbox/
    workspace_<workspace_id>/
      <project_key>/
```

The Windows bridge synchronizes server assets into a device-local inbox and
reports the corresponding Windows paths. CDP file upload must use those local
paths. A Linux path sent to Chrome running on Windows is invalid.

All generated assets need explicit provenance:

- stage
- variant index
- semantic role
- reference index when relevant
- generating provider or browser source
- local file path
- workspace and user owner

Do not select assets by filename alone. Do not fall back from a missing visual
preview to an uploaded product image.

## 5. Security

- API keys are platform-managed, encrypted credentials.
- Provider/model capability is deployed code plus platform model switches, not
  arbitrary scope text entered while creating a key.
- Disabling a key removes it from routing immediately.
- Disabling a provider/model route prevents manual and automatic selection.
- Do not log secrets, authorization headers, cookies, passwords, or ChatGPT
  session content.
- Browser automation must not bypass CAPTCHA, MFA, purchasing, subscription,
  or account-security controls.
- LLM output is untrusted. Validate schema, stage, project ID, counts,
  references, language, duration, and next stage before persistence.

## 6. Design Philosophy

The desired system is unattended but not unbounded:

- Deterministic state machine around probabilistic models.
- API first, browser fallback.
- Idempotency before retry.
- Leases and heartbeats before recovery.
- Bounded retries followed by an explicit pause with a useful reason.
- Resume from a verified checkpoint, not from default form values.
- Preserve successful work while repairing only the failed variant or stage.
- Prefer local, durable files over expiring remote URLs.
- Make user-visible outputs primary; diagnostic assets are secondary and
  collapsed in the UI.

## 7. User-Facing Outcomes

The Content Factory's primary outputs are:

1. The requested number of complete videos.
2. One matching editing/publishing guide for each complete video.
3. Batch download of videos and guides.

Project assets, stage envelopes, provider tasks, and repair history are useful
for diagnostics but must not dominate the normal UI.
