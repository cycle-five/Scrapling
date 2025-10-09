# Google OAuth Testing Guide

This guide explains how to test Google OAuth automation using the provided test files.

## Quick Start (No Google Setup Required)

The simplest way to test the button detection and clicking logic:

### Step 1: Start the test server

```bash
cd /home/lothrop/src/Scrapling.worktrees/dev
python -m http.server 8000
```

### Step 2: Run the test script (in a new terminal)

```bash
cd /home/lothrop/src/Scrapling.worktrees/dev
python test_google_oauth.py
```

**What happens:**
- A browser window opens (not headless)
- The script navigates to the test page
- It detects and clicks the "Sign in with Google" button
- An alert pops up confirming the button was clicked
- The browser stays open for 5 seconds so you can see the result

This tests your automation's ability to find and click Google sign-in buttons without needing actual Google OAuth credentials.

## Full Google OAuth Flow (Optional)

If you want to test the complete OAuth flow with real Google authentication:

### Step 1: Get Google OAuth Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing)
3. Navigate to "APIs & Services" > "Credentials"
4. Click "Create Credentials" > "OAuth client ID"
5. Choose "Web application"
6. Add authorized JavaScript origins:
   - `http://localhost:8000`
7. Copy the Client ID

### Step 2: Update the HTML file

Edit `test_google_oauth.html` and replace this line:

```html
data-client_id="YOUR_GOOGLE_CLIENT_ID"
```

with your actual Client ID:

```html
data-client_id="123456789-abcdefghijk.apps.googleusercontent.com"
```

### Step 3: Set your Google credentials

```bash
export GOOGLE_EMAIL="your-email@gmail.com"
export GOOGLE_PASSWORD="your-password"
```

### Step 4: Test with your actual script

```bash
python scrapling_pick.py --google-oauth
```

## Files Created

- **`test_google_oauth.html`** - Simple test page with Google sign-in button
- **`test_google_oauth.py`** - Test script that automates clicking the button
- **`GOOGLE_OAUTH_TESTING.md`** - This file (instructions)

## Testing Your Script

Once you've verified the test page works, you can test your actual automation:

```bash
# Test with username/password (default)
python scrapling_pick.py

# Test with Google OAuth
python scrapling_pick.py --google-oauth
```

## Troubleshooting

### "Could not find Google sign-in button"
- Make sure the page loaded completely
- Check if your selectors match the button on the actual site
- Try adding more selectors to the list in `google_oauth_login_page_make()`

### "This browser or app may not be secure"
- This is Google's bot detection
- Scrapling's `StealthySession` with Camoufox helps, but isn't foolproof
- Try with `humanize=True` and slower interactions
- Consider using saved browser sessions after manual first login

### Google blocks automated login
- Google actively blocks bots from logging in
- For production use, consider:
  - Manually logging in once, then reusing the session
  - Using Google's official APIs instead of scraping
  - Requesting explicit permission from Google for automation

## Next Steps

After testing locally:

1. Identify which sites in your `picks` list support Google OAuth
2. Test them individually with `headless=False` first
3. Adjust selectors if needed for each site
4. Once working, the persistent browser context will save your login across runs

## Notes

- The persistent browser context automatically saves cookies and authentication state
- After successful login once, subsequent runs should skip the login flow
- Different sites may have different button styles/selectors
- Always respect rate limits and Terms of Service
