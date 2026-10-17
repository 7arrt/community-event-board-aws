# Smoke Test Checklist — Community Event Board (POC)

Verification of the deployed infrastructure after pipeline **#962345** on `main`
(commit `4a7efb8`), region `eu-west-1`, account `888577054639`.

**Viewer endpoint:** https://d270hfj9y2vlls.cloudfront.net
**Direct ALB:** http://commun-Appli-UJv0Y3VLbxhB-2111380446.eu-west-1.elb.amazonaws.com
**Images bucket:** https://community-board-storage-imagesbucket-offkmev3o7zi.s3.eu-west-1.amazonaws.com

> The deployed application is the standard-library stub embedded in
> `infra/compute.yaml`. `/events` returning `{"events":[],"stub":true}` is the
> expected POC response — the infrastructure, not the application, is the
> deliverable (see `README.md`).

## Positive checks — the viewer serves

| # | Check | Command | Expected | Screenshot |
|---|-------|---------|----------|------------|
| 1 | Viewer health | `curl -s https://d270hfj9y2vlls.cloudfront.net/health` | `200 {"status":"ok"}` | `Viewer healthy.png` |
| 2 | Events endpoint | `curl -s https://d270hfj9y2vlls.cloudfront.net/events` | `200 {"events":[],"stub":true}` | `Events endpoint.png` |
| 3 | HTTPS enforced | `curl -I http://d270hfj9y2vlls.cloudfront.net/health` | `301` to `https://` | `Force HTTPS.png` |

## Negative checks — the security boundary holds

| # | Check | Command | Expected | Screenshot |
|---|-------|---------|----------|------------|
| 4 | Origin locked to CloudFront | `curl -s -o /dev/null -w '%{http_code}' http://commun-Appli-UJv0Y3VLbxhB-2111380446.eu-west-1.elb.amazonaws.com/health` | blocked (no `1xx`–`3xx`) | `Origin locked.png` |
| 5 | Bucket stays private | `curl -I https://community-board-storage-imagesbucket-offkmev3o7zi.s3.eu-west-1.amazonaws.com/images/probe-denied.jpg` | `403 AccessDenied` | `Bucket private.png`, `storage xml.png` |
| 6 | Target group healthy | `aws elbv2 describe-target-health --target-group-arn <arn>` | `healthy` | `Target healthy.png` |

## Result

All 6 checks passed. These mirror the automated gate in `ci/verify.sh`,
which fails the pipeline on any deviation — the screenshots are the manual,
human-readable record of the same evidence.

## Reproduce (Windows)

```
curl -s -o NUL -w "%{http_code}\n" https://d270hfj9y2vlls.cloudfront.net/health
curl -s -o NUL -w "%{http_code}\n" https://d270hfj9y2vlls.cloudfront.net/events
curl -s -o NUL -w "%{http_code}\n" http://d270hfj9y2vlls.cloudfront.net/health
curl -s -o NUL -w "%{http_code}\n" http://commun-Appli-UJv0Y3VLbxhB-2111380446.eu-west-1.elb.amazonaws.com/health
curl -s -o NUL -w "%{http_code}\n" https://community-board-storage-imagesbucket-offkmev3o7zi.s3.eu-west-1.amazonaws.com/images/probe-denied.jpg
```

> URLs are from the debug sandbox and will not resolve after it is torn down;
> the checklist is the durable record of what the pipeline verified.
