import EnvBadge from './env-badge';
import { IconBank, IconCheck, IconCloud, IconMail, IconRows, IconSpark, LogoMark } from './icons';
import Uploader from './uploader';

// Each step in plain words, with the part of the system that does it for the curious.
const HOW = [
  { icon: <IconCloud />, title: 'Secure direct upload',
    text: 'The file goes from your browser to encrypted cloud storage through a one-time link, never through an app server.',
    tech: 'S3 presigned POST' },
  { icon: <IconRows />, title: 'Every row checked',
    text: 'Each value is validated in one pass. Skipped rows come with a reason, and a file delivered twice is loaded once.',
    tech: 'Lambda · Postgres COPY' },
  { icon: <IconSpark />, title: 'Matched in seconds',
    text: 'Clear rules are compared in one database query. Only vague ones go to AI, once per group of similar customers.',
    tech: 'n8n · SQL · Gemini, cached' },
  { icon: <IconMail />, title: 'One email each',
    text: 'Everyone with a match gets one email listing the loans they may qualify for.',
    tech: 'n8n · Amazon SES' },
];

const LENDERS = [
  { name: 'BankBazaar', kind: 'Marketplace', text: 'Rates and eligibility tables for 20+ banks and NBFCs.' },
  { name: 'HDFC Bank', kind: 'Bank', text: 'Age, income, credit score and employer criteria.' },
  { name: 'Kotak Mahindra Bank', kind: 'Bank', text: 'Income tiers that depend on the customer’s relationship with the bank.' },
];

const FAQ = [
  { q: 'Is a match a loan approval?',
    a: 'No. A match means the customer meets the criteria the lender publishes. The lender still makes the decision.' },
  { q: 'What should the CSV contain?',
    a: 'One row per customer with user_id, name, email, monthly_income, credit_score, employment_status and age. Columns can come in any order and extra columns are ignored. A row that fails a check is skipped with its reason; the others are processed.' },
  { q: 'How large can the file be?',
    a: 'Up to 50 MB, about 7 lakh customers. The browser compresses it before sending, so it uploads about three times faster.' },
  { q: 'Who receives the emails?',
    a: 'In test mode, only the team’s own test inboxes, at most two per upload. The addresses in your file are never emailed.' },
  { q: 'Can a file be processed twice by accident?',
    a: 'No. Each upload is recorded once, so a repeated delivery is ignored, and each batch is matched and emailed once.' },
];

const TRUST = ['Every row validated', 'Criteria refreshed daily', 'Results in seconds'];

export default function Home() {
  return (
    <>
      <nav className="topbar" aria-label="Main">
        <div className="container topbar-inner">
          <a className="brand" href="#top">
            <LogoMark size={30} className="brand-mark" />
            <span>Loan Eligibility Engine</span>
          </a>
          <div className="topbar-links">
            <a href="#how">How it works</a>
            <a href="#lenders">Lenders</a>
            <a href="#faq">FAQ</a>
          </div>
          <EnvBadge />
        </div>
      </nav>

      <div className="container page" id="top">
        <header className="hero">
          <p className="eyebrow-pill"><span aria-hidden="true" />Personal loan eligibility, at scale</p>
          <h1>Find every loan your customers <span className="accent">qualify&nbsp;for</span></h1>
          <p className="hero-sub">
            Upload a customer list. Each customer is checked against lenders’ published criteria, and
            everyone with a match gets an email.
          </p>
          <ul className="trust" aria-label="Highlights">
            {TRUST.map((t) => <li key={t}><IconCheck aria-hidden="true" />{t}</li>)}
          </ul>
        </header>

        <main className="hero-card">
          <Uploader />
        </main>

        <section className="section" id="how" aria-labelledby="how-title">
          <div className="section-head">
            <p className="eyebrow">How it works</p>
            <h2 id="how-title">From a CSV to an inbox in four steps</h2>
          </div>
          <ol className="how">
            {HOW.map((step, i) => (
              <li key={step.title}>
                <div className="how-top">
                  <span className="how-icon">{step.icon}</span>
                  <span className="how-num">0{i + 1}</span>
                </div>
                <p className="how-title">{step.title}</p>
                <p className="how-text">{step.text}</p>
                <p className="how-tech">{step.tech}</p>
              </li>
            ))}
          </ol>
        </section>

        <section className="section" id="lenders" aria-labelledby="lenders-title">
          <div className="section-head">
            <p className="eyebrow">Sources</p>
            <h2 id="lenders-title">Criteria straight from the lenders</h2>
            <p className="section-sub">Read once a day from public pages, following each site’s robots.txt. A page that hasn’t changed is skipped without any AI call.</p>
          </div>
          <ul className="lenders">
            {LENDERS.map((lender) => (
              <li key={lender.name}>
                <div className="lender-head">
                  <span className="lender-icon"><IconBank /></span>
                  <span className="lender-kind">{lender.kind}</span>
                </div>
                <p className="lender-name">{lender.name}</p>
                <p className="lender-text">{lender.text}</p>
              </li>
            ))}
          </ul>
        </section>

        <section className="section section-split" id="faq" aria-labelledby="faq-title">
          <div className="section-head">
            <p className="eyebrow">FAQ</p>
            <h2 id="faq-title">Questions, answered</h2>
            <p className="section-sub">Short answers about matches, files and emails.</p>
          </div>
          <div className="faq">
            {FAQ.map((item) => (
              <details key={item.q}>
                <summary>{item.q}</summary>
                <p>{item.a}</p>
              </details>
            ))}
          </div>
        </section>

        <footer className="footer">
          <span className="footer-brand"><LogoMark size={20} className="brand-mark" />Loan Eligibility Engine</span>
          <span>A match is not a loan approval.</span>
          <a href="/sample-users.csv" download>Sample CSV</a>
        </footer>
      </div>
    </>
  );
}
