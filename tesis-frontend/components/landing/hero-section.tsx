"use client";

import { useEffect, useState } from "react";
import { useScrollReveal } from "@/hooks/use-scroll-reveal";
import { HERO } from "@/lib/constants";

const chatScenarios = [
  {
    pillar: "Estudiantes",
    question: "Cuando abren las inscripciones a materias?",
    response: "En el calendario academico: del 4 al 11 de marzo.",
  },
  {
    pillar: "Graduados",
    question: "Como pido mi analitico digital?",
    response: "Ingresas a Tramites > Graduados > Certificaciones.",
  },
  {
    pillar: "Docentes",
    question: "Donde cargo el acta final?",
    response: "En Gestion Docente > Actas > Carga de cierre.",
  },
] as const;

type ChatPhase = "userTyping" | "thinking" | "botTyping" | "pause";

export function HeroSection() {
  const sectionRef = useScrollReveal(0.1);
  const [scenarioIndex, setScenarioIndex] = useState(0);
  const [displayUserText, setDisplayUserText] = useState("");
  const [displayBotText, setDisplayBotText] = useState("");
  const [phase, setPhase] = useState<ChatPhase>("userTyping");

  useEffect(() => {
    const scenario = chatScenarios[scenarioIndex];
    const timers: Array<ReturnType<typeof setTimeout>> = [];
    let userInterval: ReturnType<typeof setInterval> | null = null;
    let botInterval: ReturnType<typeof setInterval> | null = null;

    setDisplayUserText("");
    setDisplayBotText("");
    setPhase("userTyping");

    let userCharIndex = 0;
    userInterval = setInterval(() => {
      if (userCharIndex <= scenario.question.length) {
        setDisplayUserText(scenario.question.slice(0, userCharIndex));
        userCharIndex += 1;
        return;
      }

      if (userInterval) {
        clearInterval(userInterval);
      }

      setPhase("thinking");

      timers.push(
        setTimeout(() => {
          setPhase("botTyping");
          let botCharIndex = 0;
          botInterval = setInterval(() => {
            if (botCharIndex <= scenario.response.length) {
              setDisplayBotText(scenario.response.slice(0, botCharIndex));
              botCharIndex += 1;
              return;
            }

            if (botInterval) {
              clearInterval(botInterval);
            }

            setPhase("pause");
            timers.push(
              setTimeout(() => {
                setScenarioIndex((prev) => (prev + 1) % chatScenarios.length);
              }, 2600),
            );
          }, 70);
        }, 950),
      );
    }, 82);

    return () => {
      if (userInterval) {
        clearInterval(userInterval);
      }
      if (botInterval) {
        clearInterval(botInterval);
      }
      timers.forEach((timer) => clearTimeout(timer));
    };
  }, [scenarioIndex]);

  const activeScenario = chatScenarios[scenarioIndex];

  return (
    <section
      ref={sectionRef}
      className="reveal relative flex min-h-screen flex-col items-center justify-center px-6 pt-24 pb-16"
    >
      <div className="relative z-10 mx-auto max-w-5xl text-center">
        <div className="reveal-scale mb-8 inline-flex items-center gap-2 rounded-full glass px-5 py-2.5">
          <span className="h-2 w-2 rounded-full bg-accent progress-pulse" />
          <span className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
            {HERO.preHeadline}
          </span>
        </div>

        <h1 className="text-balance text-5xl font-bold leading-[1.1] tracking-tight md:text-7xl lg:text-8xl">
          <span className="text-foreground">Convierte tu sitio web</span>
          <br />
          <span className="gradient-text">en una conversacion</span>
        </h1>

        <p className="mx-auto mt-8 max-w-2xl text-pretty text-lg leading-relaxed text-muted-foreground md:text-xl">
          {HERO.subHeadline}
        </p>

        <div className="mx-auto mt-16 max-w-lg">
          <div className="glass glow-card rounded-3xl p-1">
            <div className="rounded-[22px] bg-card/80 p-6">
              <div className="flex items-center gap-3 border-b border-border/50 pb-4">
                <div className="flex gap-1.5">
                  <div className="h-3 w-3 rounded-full bg-gradient-to-br from-red-300 to-red-400" />
                  <div className="h-3 w-3 rounded-full bg-gradient-to-br from-yellow-300 to-amber-400" />
                  <div className="h-3 w-3 rounded-full bg-gradient-to-br from-green-300 to-green-400" />
                </div>
                <span className="ml-2 font-mono text-xs text-muted-foreground/60">widget.4gentle.io</span>
              </div>

              <div className="mt-4 flex items-center justify-between">
                <span className="rounded-full bg-primary/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-primary">
                  {activeScenario.pillar}
                </span>
                <div className="flex items-center gap-1.5">
                  {chatScenarios.map((scenario) => (
                    <span
                      key={scenario.pillar}
                      className={`h-1.5 rounded-full transition-all duration-300 ${
                        scenario.pillar === activeScenario.pillar ? "w-8 bg-primary" : "w-2 bg-border"
                      }`}
                    />
                  ))}
                </div>
              </div>

              <div className="mt-5 h-[186px] space-y-4">
                <div className="flex min-h-[62px] justify-end">
                  <div className="max-w-[88%] rounded-2xl rounded-br-sm bg-gradient-to-r from-primary to-accent px-5 py-3 text-left text-sm text-primary-foreground shadow-lg shadow-primary/10">
                    {displayUserText}
                    {phase === "userTyping" && (
                      <span className="ml-0.5 inline-block h-4 w-[2px] bg-primary-foreground/70 animate-pulse" />
                    )}
                  </div>
                </div>

                <div className="flex min-h-[66px] justify-start">
                  <div className="flex min-h-[66px] w-full max-w-[88%] items-center rounded-2xl rounded-bl-sm glass px-5 py-3 text-left text-sm text-foreground">
                    {phase === "thinking" ? (
                      <div className="typing-dots" aria-label="esperando respuesta">
                        <span />
                        <span />
                        <span />
                      </div>
                    ) : (
                      <>
                        {displayBotText}
                        {phase === "botTyping" && (
                          <span className="ml-0.5 inline-block h-4 w-[2px] bg-foreground/60 animate-pulse" />
                        )}
                      </>
                    )}
                  </div>
                </div>

                <div className="flex items-center gap-2 pt-1">
                  <div className="h-2 w-2 rounded-full bg-gradient-to-r from-primary to-accent progress-pulse" />
                  <span className="font-mono text-[11px] text-muted-foreground/50">
                    {phase === "thinking" ? "Buscando respuesta..." : "Respuesta en 1.1s | 3,400 paginas indexadas"}
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
