// Code node "Attach cohort refs" (Workflow B and the warm step). Mode: Run Once for All Items.
//
// The LLM gateway answers every request in one call and returns one result per request, in
// request order (request_index), with only { ok, data, calls, tokens, ... }. This puts each
// request's refs back next to its answer: cohort_refs (question id -> criterion_key + signature)
// for a judging request, or criteria_refs (rule id -> criterion) for a compile request, so
// "Collect verdicts" can map every answer.
const requests = $('Build cohort requests').all().map((item) => item.json);
return $input.all().map((item, i) => {
  const request = requests[item.json.request_index ?? i];
  const refs = request.criteria_refs ? { criteria_refs: request.criteria_refs } : { cohort_refs: request.cohort_refs };
  return { json: { ...item.json, ...refs } };
});
