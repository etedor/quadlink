"""The live quad page: the four relay slots playing side by side."""

QUAD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>quadlink · quad</title>
<style>
  html, body { margin: 0; height: 100%; background: #000; }
  main { display: grid; grid-template: 1fr 1fr / 1fr 1fr; gap: 2px; height: 100vh; }
  .tile { position: relative; background: #0a0c10; overflow: hidden; }
  video { width: 100%; height: 100%; object-fit: contain; display: block; }
  .tile button { all: unset; position: absolute; inset: 0; cursor: pointer; }
  .tile .badge { position: absolute; left: 8px; top: 8px; padding: 2px 8px; border-radius: 6px;
    background: rgba(0, 0, 0, 0.6); color: #c9cfda; font: 12px system-ui, sans-serif; }
  .tile.loud .badge { background: #1f3a66; color: #fff; }
  @media (max-aspect-ratio: 1/1) { main { grid-template: repeat(4, 1fr) / 1fr; } }
</style>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
</head>
<body>
<main>
  <div class="tile">
    <video muted autoplay playsinline></video>
    <button aria-label="Unmute slot 1"></button>
    <span class="badge">1</span>
  </div>
  <div class="tile">
    <video muted autoplay playsinline></video>
    <button aria-label="Unmute slot 2"></button>
    <span class="badge">2</span>
  </div>
  <div class="tile">
    <video muted autoplay playsinline></video>
    <button aria-label="Unmute slot 3"></button>
    <span class="badge">3</span>
  </div>
  <div class="tile">
    <video muted autoplay playsinline></video>
    <button aria-label="Unmute slot 4"></button>
    <span class="badge">4</span>
  </div>
</main>
<script>
  const tiles = [...document.querySelectorAll('.tile')];

  function play(video, src) {
    // native first: iOS Safari has HLS built in but no MSE, so hls.js can't run there
    if (video.canPlayType('application/vnd.apple.mpegurl') || !window.Hls || !Hls.isSupported()) {
      video.src = src;
      const retry = () => setTimeout(() => play(video, src), 3000);
      video.addEventListener('error', retry, { once: true });
      return;
    }
    const hls = new Hls({ liveSyncDurationCount: 3 });
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (!data.fatal) return;
      hls.destroy();
      setTimeout(() => play(video, src), 3000);
    });
    hls.loadSource(src);
    hls.attachMedia(video);
  }

  tiles.forEach((tile, i) => {
    const video = tile.querySelector('video');
    play(video, '/streams/' + (i + 1));
    // one tile has sound at a time; click the loud one again to mute it
    tile.querySelector('button').addEventListener('click', () => {
      const loud = !tile.classList.contains('loud');
      tiles.forEach(t => { t.classList.remove('loud'); t.querySelector('video').muted = true; });
      if (loud) { tile.classList.add('loud'); video.muted = false; video.play(); }
    });
  });
</script>
</body>
</html>
"""
