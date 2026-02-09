import { Navbar } from "@/components/landing/navbar";
import { HeroSection } from "@/components/landing/hero-section";
import { ProblemSection } from "@/components/landing/problem-section";
import { SolutionSection } from "@/components/landing/solution-section";
import { FeaturesSection } from "@/components/landing/features-section";
import { TechSection } from "@/components/landing/tech-section";
import { ClosingSection } from "@/components/landing/closing-section";
import { Footer } from "@/components/landing/footer";
import { AnimatedBg } from "@/components/ui/animated-bg";

export default function Page() {
  return (
    <>
      <AnimatedBg />
      <Navbar />
      <main className="relative z-10">
        <HeroSection />
        <ProblemSection />
        <SolutionSection />
        <FeaturesSection />
        <TechSection />
        <ClosingSection />
      </main>
      <Footer />
    </>
  );
}
