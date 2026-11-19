// Confirmation prompts, navigation toggle and "still working" feedback (NFR-06)
document.addEventListener('submit', function (e) {
  var f = e.target;
  var msg = f.getAttribute && f.getAttribute('data-confirm');
  if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
  var busy = document.getElementById('busy');
  if (busy) {
    setTimeout(function () { busy.classList.remove('hidden'); }, 3000);
  }
  var btns = f.querySelectorAll('button[type=submit]');
  setTimeout(function () { btns.forEach(function (b) { b.disabled = true; }); }, 0);
});
document.addEventListener('click', function (e) {
  var t = e.target.closest && e.target.closest('[data-toggle="nav"]');
  if (t) { var n = document.getElementById('nav'); if (n) n.classList.toggle('open'); }
});
window.addEventListener('pageshow', function () {
  document.querySelectorAll('button[type=submit]').forEach(function (b) { b.disabled = false; });
  var busy = document.getElementById('busy'); if (busy) busy.classList.add('hidden');
});
