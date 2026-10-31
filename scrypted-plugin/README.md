# Scrypted Stamina

Turns every camera's detections (the camera's own AI, Coral Guard, any other detector) into events with subjects and verdicts, cuts fMP4 segments from the Rebroadcast prebuffer at the first detection, and serves events, segments, thumbnails, clips and recordings to the Scrypted Stamina Home Assistant integration over HTTP.

Enable the **Scrypted Stamina** extension on each camera, then copy the Integration URL and API token from this plugin's settings into the Home Assistant integration. Roles (trigger, verifier, enricher, ignore) and class maps are set per source plugin in the settings and apply to every camera.

Documentation: https://github.com/marl1w/scrypted-stamina-ha (README, docs/api.md, docs/architecture.md).
