# Docker Hub Push Guide — mailroom-reloaded

**Issue:** You're not authenticated to Docker Hub  
**Status:** ✅ Fixable in 3 steps

---

## Quick Diagnosis

Your Docker setup shows:
- ✅ Docker images built locally (`mailroom-reloaded:latest`, `mailroom-reloaded-dev:latest`)
- ❌ **NOT logged into Docker Hub** (no authentication configured in `~/.docker/config.json`)
- ✅ Docker daemon is running and healthy

---

## Why This Happens

Docker Hub requires authentication to push images. Your `~/.docker/config.json` shows:
```json
{
  "auths": {},           // ← Empty! No credentials stored
  "credsStore": "desktop"
}
```

This means Docker doesn't know your Docker Hub credentials.

---

## Solution: 3-Step Push Process

### Step 1: Create a Docker Hub Account (if you don't have one)

1. Go to https://hub.docker.com
2. Click **Sign Up**
3. Create account with:
   - Username (e.g., `luciusjmorningstar`)
   - Email
   - Password
4. Verify your email

### Step 2: Generate Personal Access Token (PAT)

**Why?** More secure than password. Recommended by Docker.

1. Log in to https://hub.docker.com
2. Click your **profile icon** → **Account Settings**
3. Go to **Security** → **Personal access tokens**
4. Click **Generate new token**
5. Name: `mailroom-reloaded-push`
6. Access: Select **Read, Write, Delete** (for pushing)
7. Copy the token (you won't see it again!)
8. Save it securely

### Step 3: Log In to Docker Hub from CLI

**Option A: Interactive Login (Recommended)**
```bash
docker login
```
Then enter:
- **Username:** your Docker Hub username
- **Password:** your Personal Access Token (paste the token from Step 2)

**Option B: Non-Interactive (Less Secure)**
```bash
echo "YOUR_PAT" | docker login -u "YOUR_USERNAME" --password-stdin
```

**Option C: Use Token File**
```bash
docker login -u "YOUR_USERNAME" --password-stdin < ~/my-token.txt
```

---

## Step 4: Tag Your Image

Before pushing, tag the image with your Docker Hub username:

```bash
# Format: docker tag <local-image>:<tag> <username>/<repo>:<tag>

# For production image
docker tag mailroom-reloaded:latest <YOUR_USERNAME>/mailroom-reloaded:latest
docker tag mailroom-reloaded:latest <YOUR_USERNAME>/mailroom-reloaded:v0.2.0

# For dev image
docker tag mailroom-reloaded-dev:latest <YOUR_USERNAME>/mailroom-reloaded-dev:latest
```

**Example:**
```bash
docker tag mailroom-reloaded:latest luciusjmorningstar/mailroom-reloaded:latest
```

---

## Step 5: Push to Docker Hub

```bash
# Push production image
docker push <YOUR_USERNAME>/mailroom-reloaded:latest
docker push <YOUR_USERNAME>/mailroom-reloaded:v0.2.0

# Push dev image
docker push <YOUR_USERNAME>/mailroom-reloaded-dev:latest
```

**Example:**
```bash
docker push luciusjmorningstar/mailroom-reloaded:latest
```

**Output will look like:**
```
The push refers to repository [docker.io/luciusjmorningstar/mailroom-reloaded]
d41234ef1234: Pushing [==============>                    ] 45MB/120MB
abc5678def90: Pushed
xyz1234ab567: Pushed
...
latest: digest: sha256:abc123... size: 5432
```

---

## Verify Success

Check Docker Hub:
1. Go to https://hub.docker.com
2. Search for `<YOUR_USERNAME>/mailroom-reloaded`
3. You should see your image with tags (latest, v0.2.0, etc.)

Or verify via CLI:
```bash
docker pull <YOUR_USERNAME>/mailroom-reloaded:latest
```

---

## Complete Example (Copy & Paste)

Replace `YOUR_USERNAME` with your Docker Hub username and `YOUR_PAT` with your token:

```bash
# Step 1: Log in
echo "YOUR_PAT" | docker login -u "YOUR_USERNAME" --password-stdin

# Step 2: Tag images
docker tag mailroom-reloaded:latest YOUR_USERNAME/mailroom-reloaded:latest
docker tag mailroom-reloaded:latest YOUR_USERNAME/mailroom-reloaded:v0.2.0
docker tag mailroom-reloaded-dev:latest YOUR_USERNAME/mailroom-reloaded-dev:latest

# Step 3: Push
docker push YOUR_USERNAME/mailroom-reloaded:latest
docker push YOUR_USERNAME/mailroom-reloaded:v0.2.0
docker push YOUR_USERNAME/mailroom-reloaded-dev:latest

# Step 4: Verify
docker pull YOUR_USERNAME/mailroom-reloaded:latest
```

---

## Troubleshooting

### "unauthorized: authentication required"
**Solution:** You're not logged in. Run `docker login` first.

### "denied: requested access to the resource is denied"
**Solution:** Your PAT doesn't have permission. Generate a new token with **Read, Write, Delete**.

### "image not found"
**Solution:** Tag the image first: `docker tag mailroom-reloaded:latest YOUR_USERNAME/mailroom-reloaded:latest`

### "no basic auth credentials"
**Solution:** Your credentials weren't saved. Run `docker login` again with the `--password-stdin` flag.

### "500 Server Error"
**Solution:** Docker Hub might be having issues. Wait a few minutes and retry.

### "layer already exists"
**Solution:** Not an error! Docker skips layers that already exist. Your push will still complete.

---

## Security Best Practices

✅ **DO:**
- Use Personal Access Tokens (PAT), not passwords
- Grant minimal permissions (Read, Write, Delete only what's needed)
- Rotate tokens periodically
- Never commit credentials to Git
- Use `--password-stdin` instead of `-p` flag

❌ **DON'T:**
- Use your Docker Hub password in CI/CD
- Commit tokens to Git
- Share your PAT
- Use the same token everywhere

---

## Automated Push Script

Save this as `scripts/push-docker.sh`:

```bash
#!/bin/bash
set -e

USERNAME="${1:-luciusjmorningstar}"
VERSION="${2:-latest}"

echo "🐳 Tagging mailroom-reloaded images..."
docker tag mailroom-reloaded:latest $USERNAME/mailroom-reloaded:$VERSION
docker tag mailroom-reloaded:latest $USERNAME/mailroom-reloaded:latest
docker tag mailroom-reloaded-dev:latest $USERNAME/mailroom-reloaded-dev:$VERSION

echo "📤 Pushing to Docker Hub..."
docker push $USERNAME/mailroom-reloaded:$VERSION
docker push $USERNAME/mailroom-reloaded:latest
docker push $USERNAME/mailroom-reloaded-dev:$VERSION

echo "✅ Push complete!"
echo ""
echo "Pull with:"
echo "  docker pull $USERNAME/mailroom-reloaded:$VERSION"
echo "  docker pull $USERNAME/mailroom-reloaded-dev:$VERSION"
```

Usage:
```bash
bash scripts/push-docker.sh luciusjmorningstar v0.2.0
```

---

## Docker Hub Organization Push (Optional)

If pushing to an organization instead of personal account:

```bash
docker tag mailroom-reloaded:latest my-org/mailroom-reloaded:latest
docker push my-org/mailroom-reloaded:latest
```

You need to be an **owner** or have **admin** permissions in the org.

---

## Next Steps

1. ✅ Create Docker Hub account (if needed)
2. ✅ Generate Personal Access Token
3. ✅ Run `docker login`
4. ✅ Tag images: `docker tag mailroom-reloaded:latest YOUR_USERNAME/mailroom-reloaded:latest`
5. ✅ Push: `docker push YOUR_USERNAME/mailroom-reloaded:latest`
6. ✅ Verify on Docker Hub

---

## Current Your Images

You have these images ready to push:

| Image | Size | Status |
|-------|------|--------|
| `mailroom-reloaded:latest` | 373MB | ✅ Ready |
| `mailroom-reloaded-dev:latest` | 828MB | ✅ Ready |

Once you're logged in and tagged, both are ready to push.

---

**Status: Ready to push once authenticated** ✅

Need help with any step? See troubleshooting section above.
