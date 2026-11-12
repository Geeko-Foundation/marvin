/** Builds the one stylesheet the app serves, from the classes it actually uses. */
module.exports = {
  content: [
    './templates/**/*.html',
    './static/js/**/*.js',
    // Python that hands class names to templates: badge colours, table styles.
    './tsp/**/*.py',
    './profile/**/*.py',
    './payments/**/*.py',
  ],
  // Assembled in templates as alert-{{ tag }}, so the full names never appear.
  safelist: ['alert-error', 'alert-success', 'alert-warning', 'alert-info'],
  plugins: [require('daisyui')],
  daisyui: {
    // Every theme, so a fork can pick any of them through BRAND_THEME_* alone.
    themes: true,
  },
};
