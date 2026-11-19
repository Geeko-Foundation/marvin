# CSS build

This folder contains the npm/Tailwind CSS build. Run these commands from the project root (Node.js and npm are required):

```sh
npm --prefix css_build ci
npm --prefix css_build run build
cp css_build/target/styles.css static/css/styles.css
```

`npm ci` downloads the locked dependencies into `css_build/node_modules`. This folder is generated locally and can be recreated by running the same command.

`package.json` defines `build` (minified output) and `watch` (rebuild on source changes); `package-lock.json` pins the npm dependencies. After changing templates or CSS sources, run the build and copy the output again. Django serves `static/css/styles.css`, as linked in `templates/partials/head_assets.html`—it does not serve the generated `target/styles.css` directly.

The build starts at `app.css`: it imports Tailwind, `src/themes.css` (the DaisyUI `i-layer-light` and `i-layer-dark` themes), and `src/custom.css`, then scans the templates, `static/js` and the app Python (which hands badge classes to templates) for utility classes and enables DaisyUI. Only those paths are scanned, so the build is the same on every machine. Edit those CSS sources and the templates rather than `target/styles.css`, which is generated. `src/custom.css` holds project-specific CSS, such as the print rules.

To watch while editing, run `npm --prefix css_build run watch` from the project root. Watch writes only to `css_build/target/styles.css`; copy it to `static/css/styles.css` after stopping the watcher, or from another terminal, to make the changes available to Django.
