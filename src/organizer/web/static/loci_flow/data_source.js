import fixtureState from "./fixtures.json" with { type: "json" };
import {
  createStageB1Fixture,
  DEFAULT_STAGE_B1_SCENARIO_ID,
} from "./stage_b1_scenarios.js";

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function selectedScenarioId(explicitScenarioId) {
  return explicitScenarioId
    || globalThis.__LOCI_FLOW_STAGE_B1_SCENARIO__
    || DEFAULT_STAGE_B1_SCENARIO_ID;
}

export function createFixtureDataSource(options = {}) {
  const scenarioId = selectedScenarioId(options.scenarioId);
  return Object.freeze({
    kind: "fixture",
    scenarioId,
    load() {
      return createStageB1Fixture(clone(fixtureState), scenarioId);
    },
  });
}
