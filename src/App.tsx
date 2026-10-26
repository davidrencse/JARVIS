import { useEffect } from "react";
import { Nav } from "./components/Nav";
import { ScrollIsland } from "./components/scroll-island";
import { interceptHashLinks, scrollToId } from "./lib/scroll";
import Hero from "./sections/Hero";
import Voice from "./sections/Voice";
import Pipeline from "./sections/Pipeline";
import Memory from "./sections/Memory";
import Knowledge from "./sections/Knowledge";
import Actions from "./sections/Actions";
import Cost from "./sections/Cost";
import Everywhere from "./sections/Everywhere";
import Principles from "./sections/Principles";
import Faq from "./sections/Faq";
import EarlyAccess from "./sections/EarlyAccess";
import Footer from "./sections/Footer";

const TOPICS = [
  { id: "voice", title: "Voice check" },
  { id: "how", title: "How it works" },
  { id: "memory", title: "Memory and tasks" },
  { id: "knowledge", title: "Knowledge engine" },
  { id: "actions", title: "Actions" },
  { id: "cost", title: "Cost and routing" },
  { id: "everywhere", title: "Every machine" },
  { id: "privacy", title: "Privacy" },
  { id: "faq", title: "Questions" },
];

export default function App() {
  useEffect(() => interceptHashLinks(), []);

  useEffect(() => {
    const id = decodeURIComponent(location.hash.slice(1));
    if (!id) return;
    let live = true;
    requestAnimationFrame(() => requestAnimationFrame(() => live && scrollToId(id, { instant: true })));
    return () => {
      live = false;
    };
  }, []);

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <Nav />
      <main id="main">
        <Hero />
        <Voice />
        <Pipeline />
        <Memory />
        <Knowledge />
        <Actions />
        <Cost />
        <Everywhere />
        <Principles />
        <Faq />
        <EarlyAccess />
      </main>
      <Footer />
      <ScrollIsland
        topics={TOPICS}
        showAfter="top"
        hideAt="access"
        cta={{ href: "#access", label: "Request early access" }}
      />
    </>
  );
}
