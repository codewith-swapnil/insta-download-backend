# Instagram Downloader Backend

A Flask-based REST API to download media from Instagram (posts, reels, stories, profile pictures) using `instaloader`.

## Features

- Download **posts**, **reels**, **carousels** (multiple images/videos)
- Download **stories** (requires login)
- Download **profile pictures** with basic profile info
- Automatic rate limiting (15 requests per minute per IP)
- Supports public profiles without login
- JSON response format

## Requirements

- Python 3.8+
- pip
- Git (optional)

## Installation

### 1. Clone the repository
```bash
git clone https://github.com/codewith-swapnil/insta-download-backend.git
cd insta-download-backend
```

### 2. Create a virtual environment
```bash
python -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
```

### 3. Install dependencies
```bash
pip install flask flask_cors instaloader
```
Or if you have `requirements.txt`:
```bash
pip install -r requirements.txt
```

### 4. (Optional) Enable story download by adding Instagram login
Edit `app.py` and uncomment the login line:
```python
login_instagram("YOUR_USERNAME", "YOUR_PASSWORD")
```

**Note:** Stories from private accounts or expired stories will not be downloadable.

## Running the Server

```bash
python app.py
```

The server will start at `http://0.0.0.0:5000`

## API Documentation

### Health Check
**GET** `/api/health`
```json
{
  "status": "ok",
  "logged_in": false
}
```

### Download Media
**POST** `/api/download`

**Headers:** `Content-Type: application/json`

**Request Body:**
```json
{
  "url": "https://www.instagram.com/p/xxxxx/"
}
```

**Supported URL types:**
- Post: `https://instagram.com/p/CODE`
- Reel: `https://instagram.com/reel/CODE`
- Carousel: `https://instagram.com/p/CODE` (auto-detected)
- Story: `https://instagram.com/stories/USERNAME/STORY_ID`
- Profile: `https://instagram.com/USERNAME`

## Response Examples

### Single Image Post
```json
{
  "success": true,
  "type": "image",
  "info": {
    "shortcode": "CODE",
    "caption": "Caption text...",
    "likes": 1234,
    "is_video": false,
    "timestamp": "2024-01-15T10:30:00",
    "owner": "username",
    "thumbnail": "https://..."
  },
  "data": [
    {
      "type": "image",
      "url": "https://..."
    }
  ]
}
```

### Video / Reel
```json
{
  "success": true,
  "type": "video",
  "info": { ... },
  "data": [
    {
      "type": "video",
      "url": "https://...",
      "thumbnail": "https://...",
      "views": 5000
    }
  ]
}
```

### Carousel (multiple items)
```json
{
  "success": true,
  "type": "carousel",
  "count": 5,
  "info": { ... },
  "data": [
    { "type": "image", "url": "..." },
    { "type": "video", "url": "...", "thumbnail": "..." }
  ]
}
```

### Profile Picture
```json
{
  "success": true,
  "type": "profile",
  "info": {
    "username": "username",
    "full_name": "Full Name",
    "followers": 1000,
    "following": 500,
    "posts": 200,
    "bio": "Bio text",
    "is_private": false,
    "is_verified": false
  },
  "data": [
    {
      "type": "image",
      "url": "https://..."
    }
  ]
}
```

## Rate Limiting

- **15 requests per minute** per IP address
- Response status `429` with `retry_after` field when limit exceeded

## Error Responses

| Status | Meaning |
|--------|---------|
| 400 | Invalid URL or missing JSON body |
| 403 | Private account or login required |
| 404 | Post/Story/Profile not found |
| 429 | Rate limit exceeded |
| 500 | Instagram server error or unexpected issue |

## Deployment Notes

- For production, use a production WSGI server like `gunicorn`:
  ```bash
  gunicorn -w 4 -b 0.0.0.0:5000 app:app
  ```
- Set environment variables for sensitive data (Instagram login)
- Use a reverse proxy (nginx) for SSL and rate limiting

## .gitignore Example

Create a `.gitignore` file to exclude virtual environment and cache:
```gitignore
venv/
__pycache__/
*.pyc
*.json
*.log
.env
.DS_Store
```

## License

MIT License

## Disclaimer

This project is for educational purposes only. Respect Instagram's terms of service and do not use it for spam or data mining.
```

Save this as `README.md` and commit it to your repository. If you need to generate a `requirements.txt`:

```bash
pip freeze > requirements.txt
```

Then add both files to Git and push. Let me know if you want any modifications!
