# Scrypted Stamina

Unusual-event flagging and a fast, verifier-gated cloud copy for any Scrypted camera, decided in Home Assistant.

- A **Scrypted plugin** turns each camera's detections (the camera's own AI, Coral Guard, any other Scrypted detector) into events. It cuts video segments from the prebuffer at the first detection and serves recordings through Scrypted's standard interfaces.
- A **Home Assistant integration** adds context from your entities, scores how unusual each event is, uploads segments to your NAS or cloud right away, and keeps the upload only when the event is confirmed or unusual.

Status: design. See [docs/architecture.md](docs/architecture.md) and [docs/plan.md](docs/plan.md).

## Licence

MIT
