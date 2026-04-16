import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto mt-20 max-w-xl text-center">
      <p className="text-xs uppercase tracking-[0.25em] text-[var(--color-stone)]">
        404
      </p>
      <h1 className="mt-1 font-[var(--font-display)] text-4xl">Off the map</h1>
      <p className="mt-4 text-[var(--color-stone)]">
        We could not find that county (or page). Try the{" "}
        <Link className="underline" href="/">
          atlas home
        </Link>
        .
      </p>
    </div>
  );
}
