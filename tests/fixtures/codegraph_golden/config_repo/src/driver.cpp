#include "tweak.h"

void SetupPassing(TweakGroup& g) {
    g.groupName = "Passing Logic";
    g.Add(P, "AimPID_P", 1.3f);
    g.Add(Q, "hidden_key_xyz", 2.0f);  // "Steering" in a comment is not a literal
}

void SetupSteering(TweakGroup& g) {
    g.Add(S, "steer_ratio", 14.5f);
}

float GainFor(const Table& t) {
    return t.Get("gain_stage_042_coefficient");
}

void Unrelated() { log("plain text"); }
