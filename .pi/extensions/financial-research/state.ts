export interface ResearchState {
	currentCompany?: string;
	selectedAccession?: string;
	reportDraftId?: string;
	verifyAttempts?: number;
	lastDraftId?: string;
	maxVerifyAttempts?: number;
	derivedMetricRender?: "v1" | "v2";
}

export function resetVerificationState(state: ResearchState): void {
	state.verifyAttempts = 0;
	state.lastDraftId = undefined;
	state.maxVerifyAttempts = undefined;
}

export interface StateResolution {
	accession_number?: string;
	accession_from_state: boolean;
}

export function resolveAccession<T extends { accession_number?: string | null }>(
	args: T,
	state: ResearchState,
): { args: T; resolution: StateResolution } {
	if (Object.prototype.hasOwnProperty.call(args, "accession_number")) {
		return { args: { ...args }, resolution: { accession_number: args.accession_number ?? undefined, accession_from_state: false } };
	}
	if (!state.selectedAccession) {
		return { args: { ...args }, resolution: { accession_from_state: false } };
	}
	return {
		args: { ...args, accession_number: state.selectedAccession },
		resolution: { accession_number: state.selectedAccession, accession_from_state: true },
	};
}

export function updateCompanyState(state: ResearchState, company: string, accession?: string | null): void {
	state.currentCompany = company;
	if (accession) state.selectedAccession = accession;
}
