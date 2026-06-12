---
name: No More Cheaters — AI Pipeline Implementation Plan
date: 2026-06-05
status: planning
---

# AI Pipeline Plan: No More Cheaters

## 1. Overview

Build an **offline classroom-video analysis pipeline** that processes each uploaded video through two pretrained models + a temporal rules layer, producing two final outputs:

1. **Annotated review video** — bounding boxes + labels drawn on suspicious moments
2. **Timeline incident report** — `00:12:41 - PHONE_DETECTED - 0.88`

No student identity, no seat mapping, no location text. The instructor judges visually from the annotated footage.

---

## 2. AI Stack

### 2.1 Object Detector: YOLO11x

**Model:** `yolo11x.pt` (from Ultralytics, ~100MB)

**Why:** YOLO11 supports detection, pose, and tracking. The 'x' variant (extra-large) trades inference speed for accuracy — correct for offline processing where real-time isn't needed.

**Target COCO classes for exam proctoring:**

| Class Label | COCO Class | Maps To |
|---|---|---|
| `cell phone` | 67 | PHONE_DETECTED |
| `book` | 73 | SUSPICIOUS_OBJECT |
| `laptop` | 63 | SUSPICIOUS_OBJECT |
| `remote` | 75 | SUSPICIOUS_OBJECT |

### 2.2 Pose Estimator: YOLO11x-pose

**Model:** `yolo11x-pose.pt` (YOLO11 pose variant)

**Why:** Multi-person pose estimation gives body keypoints (nose, eyes, shoulders, etc.) for each person in the frame. This is what you need for classroom scenes with multiple students — not single-face webcam methods.

**Looking-away logic (temporal rules, not ML):**
- Extract nose + eye keypoints for each detected person
- Compute head orientation proxy from keypoint positions
- If head orientation deviates from "facing forward" baseline across multiple consecutive sampled frames → flag `LOOKING_AWAY`
- Only flag if sustained across ≥3 sampled frames (reduces noise from natural movement)

### 2.3 Temporal Rules Layer

Simple Python logic that merges frame-level detections into events:

```
Frame 47:  phone detected (conf 0.91)
Frame 52:  phone detected (conf 0.88)    → merge into same PHONE event
Frame 57:  phone detected (conf 0.85)    → merge into same PHONE event
Frame 112: looking away detected         → new LOOKING_AWAY event
```

**Rules:**
- Same detection class within a 5-second sliding window → single event
- Event confidence = max confidence across merged frames
- Event duration = first_frame → last_frame within the window
- Events shorter than 2 seconds are dropped (noise filter)

---

## 3. Processing Flow

```
Upload Video → Save to MEDIA_ROOT
               ↓
Queue AnalysisJob (django-rq → Redis)
               ↓
Worker picks up job
               ↓
[STEP 1] Open video with OpenCV (cv2.VideoCapture)
[STEP 2] Extract metadata: fps, total_frames, duration
[STEP 3] Sample frames at configurable rate (default: every 15 frames)
[STEP 4] For each sampled frame:
           ├─ Run YOLO11x detection  →  phone / suspicious objects
           ├─ Run YOLO11x-pose       →  keypoints for looking-away
           └─ Store raw detections in memory
[STEP 5] Merge frame detections into temporal events
[STEP 6] Generate annotated video:
           ├─ Start from original video
           ├─ For each event, draw bboxes + labels + confidence on relevant frames
           └─ Export as new MP4 in media/output/
[STEP 7] Generate timeline report (JSON + plain text)
[STEP 8] Save results to DB:
           ├─ Alert rows (one per event)
           ├─ Report row (summary + path to annotated video)
           └─ AnalysisJob status = COMPLETED
```

### Frame Sampling Strategy

| Parameter | Default | Rationale |
|---|---|---|
| Sample rate | Every 15th frame | ~0.5 Hz at 30fps — catches phone pickups and sustained looking-away |
| Min event duration | 2 seconds | Drops spurious single-frame detections |
| Max event gap | 5 seconds | Frames of same class within 5s → same event |
| Detection confidence | ≥ 0.3 | Lower threshold for higher recall; temporal merge reduces false positives |

---

## 4. Outputs

### 4.1 Annotated Review Video

- Same duration as input
- Bounding boxes + labels drawn on frames where events occur
- Color coding: red = PHONE_DETECTED, orange = SUSPICIOUS_OBJECT, yellow = LOOKING_AWAY
- Confidence score displayed on each box
- Saved as: `media/output/{job_uuid}_annotated.mp4`

### 4.2 Timeline Incident Report

**JSON format:**
```json
{
  "video_id": "uuid",
  "duration_seconds": 540,
  "events": [
    {"timestamp": "00:12:41", "seconds": 761, "type": "PHONE_DETECTED", "confidence": 0.91, "duration": 3.2},
    {"timestamp": "00:18:03", "seconds": 1083, "type": "LOOKING_AWAY", "confidence": 0.76, "duration": 4.1},
    {"timestamp": "00:31:22", "seconds": 1882, "type": "PHONE_DETECTED", "confidence": 0.88, "duration": 2.5}
  ],
  "summary": {
    "total_events": 3,
    "PHONE_DETECTED": 2,
    "LOOKING_AWAY": 1,
    "max_confidence": 0.91
  }
}
```

**Plain text format (for export):**
```
00:12:41 - PHONE_DETECTED - 0.91
00:18:03 - LOOKING_AWAY - 0.76
00:31:22 - PHONE_DETECTED - 0.88
```

---

## 5. Integration into Existing Backend

### Models (already exist — no changes needed)

| Model | Role |
|---|---|
| `Video` | Stores uploaded file, hash, metadata |
| `AnalysisJob` | Tracks processing status (QUEUED → PROCESSING → COMPLETED / FAILED) |
| `Alert` | One row per **event** (not per frame) |
| `Report` | Summary stats + path to annotated video |
| `AuditLog` | Log job start/complete |

### New File: `apis/tasks.py`

The django-rq worker function `run_analysis(job_id)`:
1. Loads the AnalysisJob + associated Video
2. Opens video with OpenCV
3. Runs YOLO11x + YOLO11x-pose on sampled frames
4. Merges frame detections into events
5. Creates Alert rows (one per event)
6. Generates annotated video
7. Creates Report with path + summary
8. Updates AnalysisJob status

### New Directory: `media/output/`

Stores generated annotated MP4 files.

### Existing Endpoints (still need to be created)

See the endpoint list from the previous plan — all still needed. The AI pipeline plugs into the analysis-jobs workflow:

```
POST /api/analysis-jobs/   → creates job, enqueues to django-rq
GET  /api/analysis-jobs/{id}/ → poll status, get result URLs
GET  /api/reports/{id}/     → get timeline report JSON
GET  /media/output/{filename}.mp4 → download annotated video
```

---

## 6. Build Order

### Phase 1 — Minimal End-to-End Pipeline (1 session)

**Goal:** One uploaded MP4 goes through the full pipeline and produces outputs.

- [ ] Install ultralytics (YOLO11x + YOLO11x-pose) — already in venv
- [ ] Write `apis/tasks.py` with `run_analysis(job_id)`:
  - Open video, sample frames
  - Run YOLO11x detection → collect phone/object detections
  - Run YOLO11x-pose → collect looking-away cues
  - Merge frame detections into events
  - Create Alert + Report rows
- [ ] Write annotated video generation (draw bboxes on frames → write new MP4)
- [ ] Write timeline report generation (JSON + text)
- [ ] Test with a single real MP4

### Phase 2 — Backend API Wrappers (1 session)

**Goal:** Create the HTTP endpoints for the full REST API.

- [ ] Write ViewSets for all models (Exam, ExamSession, Video, Alert, AnalysisJob, Report)
- [ ] Wire up DRF DefaultRouter in `apis/urls.py`
- [ ] Add video upload endpoint with format validation + SHA-256 dedup
- [ ] Add review endpoint on alerts (mark reviewed/unreviewed)
- [ ] Wire up signals: auto-create AnalysisJob on video upload
- [ ] Test with curl/Postman

### Phase 3 — Frontend Dashboard (deferred)

Frontend is deferred. When started later:

- [ ] Add `react-router-dom`, `axios` to package.json
- [ ] Login/Register pages (connect to dj-rest-auth)
- [ ] Exams list + create exam
- [ ] Exam detail page → sessions list → upload video
- [ ] Session detail page → video player + event timeline + download annotated video
- [ ] Report view → table of events

### Phase 4 — Quality + Polish (1 session)

- [ ] Temporal rule tuning (event merging, noise filtering)
- [ ] Better frame sampling strategy
- [ ] Retention management command (`cleanup_expired_videos`)
- [ ] Demo seed data command
- [ ] README with setup + run instructions

---

## 7. What We're NOT Doing

| Feature | Reason |
|---|---|
| Gaze vector / eye tracking | Too complex for remaining time; pose-based head orientation is sufficient |
| Audio analysis | Out of scope |
| Real-time / live streaming | Post-hoc only — matches thesis design |
| Custom model training | YOLO11x pretrained weights are sufficient |
| Student identity / seat mapping | You explicitly excluded this from outputs |
| Cloud storage (S3, etc.) | Local MEDIA_ROOT for demo |
| Docker / PostgreSQL | SQLite + local server for demo |

---

## 8. Technical Requirements

**Python packages (already installed in venv):**
- `ultralytics` — YOLO11x + YOLO11x-pose
- `opencv-python-headless` — frame extraction, annotated video generation
- `django-rq` + `rq` + `redis` — background job queue
- `Pillow` — image handling (installed as dependency)

**Redis (already running):**
```
redis-cli ping → PONG
```

**Worker command:**
```bash
cd no-more-cheaters-backend
.venv/bin/python manage.py rqworker default
```

---

## 9. File Changes Summary

```
no-more-cheaters-backend/
├── nomorecheaters/
│   ├── apis/
│   │   ├── views.py             [REWRITE] Full ViewSets
│   │   ├── urls.py              [REWRITE] DRF DefaultRouter
│   │   ├── tasks.py             [NEW] YOLO analysis worker
│   │   ├── signals.py           [NEW] Auto-enqueue on upload
│   │   └── apps.py              [MODIFY] Register signals
│   └── media/
│       └── output/              [NEW] Annotated videos directory
```