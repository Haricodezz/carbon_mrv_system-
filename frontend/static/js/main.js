// Auto-dismiss flash messages
setTimeout(() => {
  document.querySelectorAll('.flash').forEach(el => {
    el.style.transition = 'opacity 0.5s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 500);
  });
}, 4000);

// Pre-fill role from URL param on register page
const params = new URLSearchParams(window.location.search);
const role = params.get('role');
if (role) {
  const sel = document.querySelector('select[name="role"]');
  if (sel) sel.value = role;
}

// Confirm before dangerous actions
document.querySelectorAll('.btn-danger').forEach(btn => {
  btn.addEventListener('click', (e) => {
    if (btn.type === 'submit' && !btn.dataset.confirmed) {
      e.preventDefault();
      if (confirm('Are you sure? This action cannot be undone.')) {
        btn.dataset.confirmed = true;
        btn.click();
      }
    }
  });
});
