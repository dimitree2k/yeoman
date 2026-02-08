Your fork is now fully configured! 🎉

## Current Setup
```
origin    https://github.com/dimitree2k/nanobot.git (your fork)
upstream  https://github.com/HKUDS/nanobot (original repo)
```

## Quick Reference Commands

### Daily Workflow
```bash
git push origin main          # Push your changes to your fork
git pull origin main          # Pull from your fork
```

### Sync with Original Repo
```bash
git fetch upstream            # Get latest from original
git log HEAD..upstream/main   # See what's new
git merge upstream/main       # Merge updates into your branch
git push origin main          # Push merged updates to your fork
```

### Create a Pull Request to Original
```bash
git checkout -b feature/my-feature   # Create feature branch
# make changes...
git push origin feature/my-feature   # Push branch to your fork
# Then go to GitHub and click "Create Pull Request"
```

Your fork is live at: **https://github.com/dimitree2k/nanobot**
