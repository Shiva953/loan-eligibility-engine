// Code node "Build extraction request" (Workflow A). Mode: Run Once for All Items.
//
// S1, crawl-time compile: the page text goes to the LLM once per changed page, together with a
// strict JSON schema. The model reports values as stated on the page (amounts with their
// period, rates as written), a confidence per field group, and every remaining criterion
// classified as compilable, fuzzy or unknown_field. Unit conversion and every threshold
// decision happen afterwards in validate_products.js, as code, so the LLM never does
// arithmetic the engine depends on.
//
// The request is provider-neutral. The LLM gateway sub-workflow turns it into a Gemini (or
// other provider) call.

const PROMPT_VERSION = 'extract-v3';
const FACTS = ['monthly_income', 'credit_score', 'employment_status', 'age'];
const EMPLOYMENT = ['Salaried', 'Self-Employed', 'Business'];

const SYSTEM = `You extract personal-loan products and their eligibility rules from the text of an Indian lender or loan-comparison web page. Your output feeds an eligibility engine that knows only four facts about each applicant:
- monthly_income: net monthly income in INR
- credit_score: CIBIL score, 300 to 900
- employment_status: exactly one of "Salaried", "Self-Employed", "Business"
- age: in years

Rules:
1. Return one product per distinct lender product that the page describes with at least an interest rate or an eligibility rule. Comparison pages list many lenders: every lender with its own row in a rate table or an eligibility table is a product, even if it has no other mention, and everything the page says about the same lender (rate table row, eligibility table row, lender card) is merged into that one product. Ignore navigation, links, other loan types (home, car, gold, business, education loans) and generic advice that is not tied to a lender.
2. lender is the lender's usual name, such as "HDFC Bank" or "Aditya Birla Capital". product_name is the product as the page names it, such as "HDFC Personal Loan".
3. Interest rates are percent per annum, copied as stated. "10.99% p.a. onwards" means min 10.99 and max null.
4. List every stated minimum income in income_thresholds: amount in rupees as a plain number, and the period as written. "Rs.25,000 per month" is 25000 monthly. "Rs 5 lakh per annum" is 500000 annual. 1 lakh = 100000 and 1 crore = 10000000. employment lists the groups the threshold applies to (empty means everyone). Put any other condition, such as holding a salary account with the bank or working for a particular employer, in condition.
5. employment_allowed lists only groups the page clearly allows for this product. "salaried" maps to "Salaried". "self-employed" with no qualifier maps to "Self-Employed". "business owner" or "self-employed non-professional" maps to "Business". If the page says self-employed applicants are not eligible, list only "Salaried". If a group is described more narrowly than these three values (for example "self-employed professionals such as doctors and CAs", or "employees of private limited companies"), do not list it here. Add it to other_criteria instead. Leave employment_allowed empty when the page does not say.
6. credit_score_kind is "minimum" when the page states a score an applicant needs, must have or must meet for this lender, such as "you need a minimum credit score of 720", "CIBIL score: 685 or higher", "750 or more" or "at least 700". A stated requirement wins: if the page states a required score and elsewhere calls a score good, ideal or preferred, report the lowest required score with kind "minimum", and put the preferred score in other_criteria as compilable. Use "preferred" only when the page states no required score and only calls a score good, ideal or preferred. Otherwise use "not_stated", with min_credit_score null.
7. other_criteria holds every other eligibility condition for this product, each classified:
   - "compilable": can be checked with the four facts and an exact threshold, such as "CIBIL 750 or above preferred" or "minimum income Rs 40,000 for self-employed".
   - "fuzzy": depends only on the four facts but needs judgment to apply, such as "self-employed professionals" against employment_status "Self-Employed".
   - "unknown_field": depends on information the engine does not have, such as employer category, work experience, city, residence stability, a relationship with the bank, loan amount or existing debt.
   depends_on lists the facts involved. It is empty for unknown_field.
8. confidence, per field group: 1 when stated explicitly for this product; about 0.5 when inferred, ambiguous, or when two places on the page give this lender different values (then use the value from the lender's own card or section, and still say 0.5); 0 when not stated.
9. evidence is one short quote, under 200 characters, that supports the product's rules.
Return only products the text supports.`;

const NULLABLE_NUMBER = { type: ['number', 'null'] };
const NULLABLE_INTEGER = { type: ['integer', 'null'] };
const CONFIDENCE = { type: 'number', minimum: 0, maximum: 1 };

const SCHEMA = {
  type: 'object',
  properties: {
    products: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          lender: { type: 'string' },
          product_name: { type: 'string' },
          interest_rate_min_pct: NULLABLE_NUMBER,
          interest_rate_max_pct: NULLABLE_NUMBER,
          min_age: NULLABLE_INTEGER,
          max_age: NULLABLE_INTEGER,
          min_credit_score: NULLABLE_INTEGER,
          credit_score_kind: { type: 'string', enum: ['minimum', 'preferred', 'not_stated'] },
          income_thresholds: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                amount: { type: 'number' },
                period: { type: 'string', enum: ['monthly', 'annual'] },
                employment: { type: 'array', items: { type: 'string', enum: EMPLOYMENT } },
                condition: { type: ['string', 'null'] },
              },
              required: ['amount', 'period', 'employment', 'condition'],
              additionalProperties: false,
            },
          },
          employment_allowed: { type: 'array', items: { type: 'string', enum: EMPLOYMENT } },
          other_criteria: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                text: { type: 'string' },
                class: { type: 'string', enum: ['compilable', 'fuzzy', 'unknown_field'] },
                depends_on: { type: 'array', items: { type: 'string', enum: FACTS } },
              },
              required: ['text', 'class', 'depends_on'],
              additionalProperties: false,
            },
          },
          confidence: {
            type: 'object',
            properties: {
              interest_rate: CONFIDENCE,
              age: CONFIDENCE,
              credit_score: CONFIDENCE,
              income: CONFIDENCE,
              employment: CONFIDENCE,
            },
            required: ['interest_rate', 'age', 'credit_score', 'income', 'employment'],
            additionalProperties: false,
          },
          evidence: { type: 'string' },
        },
        required: [
          'lender', 'product_name', 'interest_rate_min_pct', 'interest_rate_max_pct',
          'min_age', 'max_age', 'min_credit_score', 'credit_score_kind', 'income_thresholds',
          'employment_allowed', 'other_criteria', 'confidence', 'evidence',
        ],
        additionalProperties: false,
      },
    },
  },
  required: ['products'],
  additionalProperties: false,
};

function buildRequest(source, config) {
  return {
    purpose: 'extract_products',
    prompt_version: PROMPT_VERSION,
    model: config.llm_model_extract,
    temperature: 0,
    max_output_tokens: 32768,
    system: SYSTEM,
    prompt: `Source: ${source.display_name} (${source.kind})\n\n${source.text}`,
    schema: SCHEMA,
  };
}

// ---- n8n entry point ----
const config = $('Load app_config').first().json.config;
return [{ json: buildRequest($input.first().json, config) }];
