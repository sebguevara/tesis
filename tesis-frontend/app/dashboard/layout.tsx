import React from "react";
import { auth } from "@clerk/nextjs/server";
import { redirect } from "next/navigation";
import { FloatingMenu } from "@/components/dashboard/floating-menu";

export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { userId } = await auth();
  if (!userId) {
    redirect("/sign-in");
  }

  return (
    <div className="relative min-h-screen overflow-hidden">
      <div className="fixed inset-0 animated-gradient-bg" />
      <div className="fixed top-1/4 -left-48 h-[600px] w-[600px] rounded-full bg-gradient-to-br from-primary/8 to-accent/5 blur-[120px] pointer-events-none" />
      <div className="fixed bottom-1/4 -right-48 h-[500px] w-[500px] rounded-full bg-gradient-to-br from-accent/8 to-primary/5 blur-[120px] pointer-events-none" />

      <main className="relative z-10 mx-auto max-w-7xl px-6 py-6">{children}</main>
      <FloatingMenu />
    </div>
  );
}
