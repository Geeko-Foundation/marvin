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
       themes: [
      {
        // opensuse themes based on https://opensuse.github.io/branding-guidelines/ (out of date!!! )
        opensuse_light: {
          "primary": "#73ba25",
          "primary-content": "#173f4f",
          "secondary": "#173f4f",
          "secondary-content": "#ffffff",
          "accent": "#35b9ab",
          "accent-content": "#173f4f",
          "neutral": "#173f4f",
          "neutral-content": "#ffffff",
          "base-100": "#ffffff",
          "base-200": "#f6f8f9",
          "base-300": "#dae3e6",
          "base-content": "#173f4f",
          "info": "#21a4d4",
          "info-content": "#ffffff",
          "success": "#73ba25",
          "success-content": "#173f4f",
          "warning": "#f59e0b",
          "warning-content": "#173f4f",
          "error": "#dc2626",
          "error-content": "#ffffff",
          "--rounded-box": "0.25rem",
          "--rounded-btn": "0.25rem",
          "--rounded-badge": "0.25rem",        },
        opensuse_dark: {
          "primary": "#73ba25",
          "primary-content": "#003543",
          "secondary": "#35b9ab",
          "secondary-content": "#003543",
          "accent": "#21a4d4",
          "accent-content": "#ffffff",
          "neutral": "#003543",
          "neutral-content": "#ffffff",
          "base-100": "#173f4f",
          "base-200": "#0f2a35",
          "base-300": "#0a1d25",
          "base-content": "#f0fdf4",
          "info": "#21a4d4",
          "info-content": "#ffffff",
          "success": "#73ba25",
          "success-content": "#003543",
          "warning": "#f59e0b",
          "warning-content": "#003543",
          "error": "#ef4444",
          "error-content": "#ffffff",
          "--rounded-box": "0.25rem",
          "--rounded-btn": "0.25rem",
          "--rounded-badge": "0.25rem",        },
      },
    ]
    // Every theme, so a fork can pick any of them through BRAND_THEME_* alone.
    //themes: true,
    },
};
