"use client";

import { useMouseGlow } from "@/hooks/use-mouse-glow";
import { useEffect, useRef } from "react";

function ConversationFlowSVG() {
  return (
    <svg
      className="absolute inset-0 h-full w-full"
      viewBox="0 0 1440 900"
      fill="none"
      preserveAspectRatio="xMidYMid slice"
      aria-hidden="true"
    >
      <g className="conversation-node node-float-1">
        <rect x="118" y="140" width="220" height="62" rx="31" fill="hsl(24 60% 48% / 0.05)" />
        <circle cx="340" cy="201" r="8" fill="hsl(24 60% 48% / 0.05)" />
      </g>

      <g className="conversation-node node-float-2">
        <rect x="1020" y="120" width="250" height="56" rx="28" fill="hsl(16 70% 55% / 0.05)" />
        <circle cx="1014" cy="170" r="7" fill="hsl(16 70% 55% / 0.05)" />
      </g>

      <g className="conversation-node node-float-3">
        <rect x="220" y="640" width="180" height="54" rx="27" fill="hsl(42 90% 58% / 0.05)" />
        <circle cx="398" cy="690" r="6" fill="hsl(42 90% 58% / 0.05)" />
      </g>

      <g className="conversation-node node-float-4">
        <rect x="960" y="600" width="240" height="64" rx="32" fill="hsl(24 60% 48% / 0.04)" />
        <circle cx="1202" cy="662" r="8" fill="hsl(24 60% 48% / 0.04)" />
      </g>

      <path
        className="conversation-path conversation-path-1"
        d="M270 172C420 238 560 224 706 198C868 170 1000 198 1146 148"
      />
      <path
        className="conversation-path conversation-path-2"
        d="M300 674C470 612 574 552 726 512C902 464 1020 522 1110 630"
      />
      <path
        className="conversation-path conversation-path-3"
        d="M364 670C488 562 554 472 662 406C808 316 928 304 1044 392"
      />

      <circle className="packet packet-1" cx="270" cy="172" r="4" />
      <circle className="packet packet-2" cx="300" cy="674" r="4" />
      <circle className="packet packet-3" cx="364" cy="670" r="4" />
    </svg>
  );
}

function FloatingParticles() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let animationId: number;
    const particles: Array<{
      x: number;
      y: number;
      vx: number;
      vy: number;
      size: number;
      opacity: number;
      color: string;
    }> = [];

    function resize() {
      if (!canvas) return;
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
    }
    resize();
    window.addEventListener("resize", resize);

    const colors = ["200, 117, 51", "208, 90, 58", "210, 168, 75"];

    for (let i = 0; i < 36; i++) {
      particles.push({
        x: Math.random() * (canvas?.width || 1440),
        y: Math.random() * (canvas?.height || 900),
        vx: (Math.random() - 0.5) * 0.25,
        vy: (Math.random() - 0.5) * 0.25,
        size: Math.random() * 2 + 0.5,
        opacity: Math.random() * 0.15 + 0.03,
        color: colors[Math.floor(Math.random() * colors.length)],
      });
    }

    function animate() {
      if (!ctx || !canvas) return;
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      for (const p of particles) {
        p.x += p.vx;
        p.y += p.vy;
        if (p.x < 0) p.x = canvas.width;
        if (p.x > canvas.width) p.x = 0;
        if (p.y < 0) p.y = canvas.height;
        if (p.y > canvas.height) p.y = 0;

        ctx.beginPath();
        ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${p.color}, ${p.opacity})`;
        ctx.fill();
      }

      for (let i = 0; i < particles.length; i++) {
        for (let j = i + 1; j < particles.length; j++) {
          const dx = particles[i].x - particles[j].x;
          const dy = particles[i].y - particles[j].y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < 150) {
            ctx.beginPath();
            ctx.moveTo(particles[i].x, particles[i].y);
            ctx.lineTo(particles[j].x, particles[j].y);
            ctx.strokeStyle = `rgba(200, 117, 51, ${0.03 * (1 - dist / 150)})`;
            ctx.lineWidth = 0.5;
            ctx.stroke();
          }
        }
      }

      animationId = requestAnimationFrame(animate);
    }

    animate();

    return () => {
      cancelAnimationFrame(animationId);
      window.removeEventListener("resize", resize);
    };
  }, []);

  return <canvas ref={canvasRef} className="absolute inset-0 pointer-events-none" aria-hidden="true" />;
}

export function AnimatedBg() {
  const mouse = useMouseGlow();

  return (
    <div className="pointer-events-none fixed inset-0 z-0 overflow-hidden" aria-hidden="true">
      <div className="animated-gradient-bg absolute inset-0" />

      <div
        className="absolute inset-0 opacity-[0.03]"
        style={{
          backgroundImage: `
            linear-gradient(hsl(24 60% 48% / 0.25) 1px, transparent 1px),
            linear-gradient(90deg, hsl(24 60% 48% / 0.25) 1px, transparent 1px)
          `,
          backgroundSize: "64px 64px",
        }}
      />

      <FloatingParticles />
      <ConversationFlowSVG />

      <div className="orb orb-1" style={{ top: "5%", left: "10%" }} />
      <div className="orb orb-2" style={{ top: "55%", right: "5%" }} />

      <div className="mouse-glow" style={{ left: mouse.x, top: mouse.y }} />

      <div
        className="absolute inset-0 opacity-[0.02]"
        style={{
          backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E")`,
        }}
      />
    </div>
  );
}
