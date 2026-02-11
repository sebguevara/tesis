import { SignIn } from "@clerk/nextjs";
import { auth } from "@clerk/nextjs/server";
import { redirect } from "next/navigation";
import Link from "next/link";

export default async function SignInPage() {
  const { userId } = await auth();

  if (userId) {
    redirect("/dashboard");
  }

  return (
    <div className="relative h-screen overflow-hidden">
      <div className="fixed inset-0 animated-gradient-bg" />
      <div className="fixed top-1/4 -left-48 h-[600px] w-[600px] rounded-full bg-gradient-to-br from-primary/10 to-accent/5 blur-[120px] pointer-events-none" />
      <div className="fixed bottom-1/4 -right-48 h-[500px] w-[500px] rounded-full bg-gradient-to-br from-accent/10 to-primary/5 blur-[120px] pointer-events-none" />

      <main className="relative z-10 mx-auto flex h-full w-full max-w-7xl items-center justify-center px-6 py-4">
        <div className="glass-strong h-full w-full max-w-5xl overflow-hidden rounded-3xl border border-border/50 shadow-2xl shadow-primary/10">
          <div className="grid h-full min-h-0 grid-cols-1 lg:grid-cols-2">
            <section className="hidden flex-col justify-between border-r border-border/40 bg-gradient-to-br from-primary/10 via-background/50 to-accent/10 p-10 lg:flex">
              <div className="space-y-5">
                <Link href="/" className="inline-flex items-center gap-2">
                  <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-primary to-accent">
                    <span className="text-sm font-bold text-primary-foreground">4g</span>
                  </div>
                  <span className="text-base font-semibold tracking-tight">4gentle</span>
                </Link>
                <div className="space-y-3">
                  <h1 className="text-3xl font-semibold leading-tight text-foreground">
                    Accede al dashboard operativo
                  </h1>
                  <p className="max-w-md text-sm leading-relaxed text-muted-foreground">
                    Ingresa con tu cuenta autorizada para gestionar crawls, revisar estado y configurar el widget.
                  </p>
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                El acceso esta restringido a usuarios habilitados por el equipo interno.
              </p>
            </section>

            <section className="flex items-center justify-center p-4 sm:p-6 lg:p-8">
              <SignIn
                path="/sign-in"
                routing="path"
                forceRedirectUrl="/dashboard"
                signUpUrl="/sign-in"
                appearance={{
                  variables: {
                    colorPrimary: "hsl(24 60% 48%)",
                    colorText: "hsl(20 30% 10%)",
                    colorTextSecondary: "hsl(25 12% 48%)",
                    colorBackground: "hsl(36 35% 99%)",
                    borderRadius: "12px",
                    fontFamily: "var(--font-sans)",
                  },
                  elements: {
                    card: "shadow-none bg-transparent",
                    rootBox: "w-full",
                    formButtonPrimary:
                      "bg-gradient-to-r from-primary to-accent hover:opacity-95 transition-opacity",
                    footerAction: "hidden",
                    footerActionLink: "hidden",
                  },
                }}
              />
            </section>
          </div>
        </div>
      </main>
    </div>
  );
}
