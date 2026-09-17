import { Suspense, lazy } from "react";

/**
 * Wraps a Lottie animation in a code-split component.
 *
 * `lottie-react` re-exports every engine build from one barrel and
 * `lottie-web`'s builds aren't side-effect-free, so a static import pulls
 * ~200 kB of player into the entry chunk for something that only appears
 * on one screen. The animation JSON itself is heavy too (the AI loader
 * carries embedded artwork). Loading both on demand keeps them out of the
 * initial bundle; they arrive while the user is already waiting on work.
 *
 * `LottieLight` rather than `Lottie`: the light build drops lottie-web's
 * expression engine, which none of this app's animations use and which is
 * the part carrying a direct `eval`.
 *
 * @param load A function returning the `import()` of the animation JSON.
 *             Must be a literal `import()` at the call site — Vite needs a
 *             static specifier to split the chunk.
 */
export function createLazyLottie(load) {
  const Player = lazy(() =>
    Promise.all([import("lottie-react"), load()]).then(([lottie, animation]) => ({
      default: (props) => <lottie.LottieLight src={animation.default} {...props} />,
    })),
  );

  return function LazyLottie({ fallback = null, ...props }) {
    return (
      <Suspense fallback={fallback}>
        <Player {...props} />
      </Suspense>
    );
  };
}
