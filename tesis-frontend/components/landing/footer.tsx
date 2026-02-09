import { BRAND, FOOTER } from "@/lib/constants";

export function Footer() {
  return (
    <footer className="relative border-t border-border/50 px-6 py-12">
      <div className="mx-auto grid max-w-7xl gap-8 md:grid-cols-[auto_1fr] md:items-start">
        <div className="flex items-center gap-3">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-accent">
            <span className="text-[10px] font-bold text-primary-foreground">4g</span>
          </div>
          <span className="text-sm font-medium text-foreground tracking-tight">
            {BRAND.name}
          </span>
        </div>

        <div className="space-y-4">
          <p className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
            {FOOTER.title}
          </p>
          <div className="flex flex-wrap gap-2">
            {FOOTER.students.map((student) => (
              <span
                key={student}
                className="rounded-full border border-border/70 bg-background/70 px-3 py-1.5 text-sm text-foreground"
              >
                {student}
              </span>
            ))}
          </div>
          <p className="max-w-3xl text-sm leading-relaxed text-muted-foreground/85">
            {FOOTER.academicNote}
          </p>
        </div>
      </div>
    </footer>
  );
}
